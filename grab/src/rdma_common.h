// =============================================================================
// rdma_common.h  —  librdmacm + ibverbs 的精簡 RC（Reliable Connection）連線樣板
// -----------------------------------------------------------------------------
// 用途：
//   把「建立一條可靠 RDMA 連線、註冊記憶體、做 RDMA 寫入」這套樣板抽出來，
//   讓 server（Spark）與 client（測試 PC）的測試程式共用，不必各自重寫。
//
// 為什麼用 rdma_cm（librdmacm）而不是純 ibverbs：
//   RoCE v2 要正確連線，得處理 GID、路徑、QP 狀態機（INIT→RTR→RTS）等繁瑣細節。
//   rdma_cm 把這些自動化（像 TCP 一樣 connect/accept），在 RoCE 上不易出錯。
//
// 名詞速記：
//   PD（Protection Domain）：保護網域，MR 與 QP 都掛在某個 PD 下。
//   CQ（Completion Queue）：完成佇列，每個送/收動作完成後在此產生一筆 wc。
//   QP（Queue Pair）：一對送/收佇列，等同一條連線的端點。
//   MR（Memory Region）：把一段記憶體註冊給網卡 DMA，產生 lkey（本地）/rkey（給遠端）。
//   WRITE_WITH_IMM：RDMA 寫入並附帶 4 bytes 立即值；遠端會收到一筆完成事件（含 imm），
//                   我們用它讓 server 知道「資料到了、這是第幾幀」。
//
// 注意：此為 v0 範本，請在實機以你環境的 rdma-core 版本編譯後微調（API 大致穩定）。
// =============================================================================
#pragma once
#include <rdma/rdma_cma.h>          // rdma_cm：rdma_create_id / resolve / connect / accept
#include <infiniband/verbs.h>       // ibverbs：ibv_reg_mr / ibv_post_send / ibv_poll_cq ...
#include <netdb.h>                  // addrinfo / getaddrinfo
#include <cstring>
#include <cstdio>
#include <cstdlib>
#include <stdexcept>
#include <string>

// 小工具：條件不成立就印 errno 並丟例外，讓上層用 try/catch 統一處理。
#define RC_CHECK(x, msg) do { if (!(x)) { perror(msg); throw std::runtime_error(msg); } } while (0)

// 透過控制訊息交換給對端的記憶體區資訊：遠端 buffer 的位址、rkey、長度、(可選)crc。
struct MrInfo { uint64_t addr; uint32_t rkey; uint32_t len; uint32_t crc; };

// Step 3：N-slot ring buffer 握手擴充（grab/IP 兩端同步更新，不搞版本協商）。
// Grab 收到此結構後，每幀寫入位置：
//   slot_id  = frame_seq % n_slots
//   write_addr = addr + (uint64_t)slot_id * slot_size
// IP 端 post_recv N 個 = N 個初始 credit；每處理完一幀補一個 post_recv = credit++。
// 背壓：credit 耗盡 → Grab 下一幀 WRITE_WITH_IMM → RNR（rnr_retry_count=7=∞）→
//   Grab poll_one() 阻塞直到 IP 補 post_recv → 自然背壓，無需額外控制通道。
struct MrInfoEx {
    uint64_t addr;       // N-slot ring buffer 基底位址（cudaHostAlloc pinned）
    uint32_t rkey;       // 整塊 buffer 的 RDMA 授權金鑰（一個 MR 涵蓋全部 N slot）
    uint32_t len;        // 整塊 buffer 大小 = n_slots * slot_size
    uint32_t crc;        // 未使用（padding，對齊舊 MrInfo 前 4 欄位）
    uint32_t n_slots;    // slot 數量（RDMA ring 深度，預設 4）
    uint32_t slot_size;  // 每個 slot 大小（bytes）= sizeof(FrameHeader) + max_payload
    uint8_t  pad[228];   // 預留擴充空間（對齊至 256 bytes）
};
static_assert(sizeof(MrInfoEx) == 256, "MrInfoEx must be 256 bytes");

struct RcConn {
    rdma_event_channel* ec   = nullptr; // 事件通道：rdma_cm 的非同步事件由此取得
    rdma_cm_id*         id   = nullptr; // 連線後的資料用 id（server 為被接受的 child id）
    rdma_cm_id*         lid  = nullptr; // server 監聽用 id
    ibv_pd*             pd   = nullptr; // 保護網域
    ibv_cq*             cq   = nullptr; // 完成佇列（送/收共用）
    ibv_comp_channel*   cc   = nullptr; // （本範本用輪詢，未使用事件式 channel）

    // -------------------------------------------------------------------------
    // 建立 QP：配置 PD、CQ，並設定 QP 容量後用 rdma_create_qp 建立。
    // 做法：rdma_cm 的 cm->verbs 已指向正確的網卡 context，直接拿來配 PD/CQ。
    // -------------------------------------------------------------------------
    void make_qp(rdma_cm_id* cm) {
        pd = ibv_alloc_pd(cm->verbs);                  RC_CHECK(pd, "ibv_alloc_pd");
        cq = ibv_create_cq(cm->verbs, 64, nullptr, nullptr, 0); RC_CHECK(cq, "ibv_create_cq"); // 深度 64
        ibv_qp_init_attr qa{};
        qa.send_cq = cq; qa.recv_cq = cq;              // 送與收完成都進同一個 CQ
        qa.qp_type = IBV_QPT_RC;                       // RC：可靠連線（保證到達、有序）
        qa.cap.max_send_wr = 32; qa.cap.max_recv_wr = 32; // 同時在途的送/收 WR 上限
        qa.cap.max_send_sge = 1; qa.cap.max_recv_sge = 1; // 每個 WR 的散佈/聚集片段數
        RC_CHECK(rdma_create_qp(cm, pd, &qa) == 0, "rdma_create_qp");
    }

    // -------------------------------------------------------------------------
    // 註冊記憶體區（MR）。access 決定網卡可對這塊記憶體做什麼：
    //   IBV_ACCESS_LOCAL_WRITE  本地可寫（收資料/送來源都需要）
    //   IBV_ACCESS_REMOTE_WRITE 允許遠端 RDMA 寫入（server 的 GPU buffer 需要）
    // 註冊 GPU 記憶體（cudaMalloc 的指標）能成功，靠的是 nvidia_peermem 模組。
    // -------------------------------------------------------------------------
    ibv_mr* reg(void* buf, size_t len, int access) {
        ibv_mr* mr = ibv_reg_mr(pd, buf, len, access);
        RC_CHECK(mr, "ibv_reg_mr（GPU 記憶體失敗多半是 nvidia_peermem 未載入）");
        return mr;
    }

    // ---- server：監聽並接受一條連線 -----------------------------------------
    // 做法：建事件通道 → 建監聽 id → bind 本地 IP/port → listen →
    //       等 CONNECT_REQUEST 事件（client 連進來）→ 為該連線建 QP。
    void serve(const char* bind_ip, const char* port) {
        ec = rdma_create_event_channel(); RC_CHECK(ec, "rdma_create_event_channel");
        RC_CHECK(rdma_create_id(ec, &lid, nullptr, RDMA_PS_TCP) == 0, "rdma_create_id"); // PS_TCP=連線式
        addrinfo* ai = nullptr; getaddrinfo(bind_ip, port, nullptr, &ai);  // 把 IP/port 轉成 sockaddr
        RC_CHECK(rdma_bind_addr(lid, ai->ai_addr) == 0, "rdma_bind_addr"); // 綁在 RDMA 介面的 IP 上
        RC_CHECK(rdma_listen(lid, 1) == 0, "rdma_listen");                 // backlog=1
        printf("[rdma] listening on %s:%s\n", bind_ip ? bind_ip : "*", port);
        rdma_cm_event* ev = nullptr;
        RC_CHECK(rdma_get_cm_event(ec, &ev) == 0 && ev->event == RDMA_CM_EVENT_CONNECT_REQUEST, "wait CONNECT_REQUEST");
        id = ev->id;            // 這個 child id 代表這條新連線
        make_qp(id);            // 在 child id 上建 QP（此時 QP 為 INIT，可預掛 RECV）
        rdma_ack_cm_event(ev);  // 每個取得的事件都要 ack
    }
    // 正式接受連線。responder/initiator 深度＝同時在途的 RDMA 讀/atomic 數（這裡用 1 足夠）。
    void accept_conn() {
        rdma_conn_param cp{}; cp.responder_resources = 1; cp.initiator_depth = 1; cp.retry_count = 7; cp.rnr_retry_count = 7;
        RC_CHECK(rdma_accept(id, &cp) == 0, "rdma_accept");
        rdma_cm_event* ev = nullptr;
        RC_CHECK(rdma_get_cm_event(ec, &ev) == 0 && ev->event == RDMA_CM_EVENT_ESTABLISHED, "wait ESTABLISHED");
        rdma_ack_cm_event(ev);
        printf("[rdma] connection established (server)\n");
    }

    // ---- client：連到 server -------------------------------------------------
    // 做法：建 id → resolve_addr（解析到達對端的本地路由）→ resolve_route →
    //       建 QP → connect → 等 ESTABLISHED。每一步都是非同步、要等對應事件。
    void connect(const char* server_ip, const char* port) {
        ec = rdma_create_event_channel(); RC_CHECK(ec, "rdma_create_event_channel");
        RC_CHECK(rdma_create_id(ec, &id, nullptr, RDMA_PS_TCP) == 0, "rdma_create_id");
        addrinfo* ai = nullptr; getaddrinfo(server_ip, port, nullptr, &ai);
        RC_CHECK(rdma_resolve_addr(id, nullptr, ai->ai_addr, 2000) == 0, "rdma_resolve_addr");
        wait_event(RDMA_CM_EVENT_ADDR_RESOLVED);
        RC_CHECK(rdma_resolve_route(id, 2000) == 0, "rdma_resolve_route");
        wait_event(RDMA_CM_EVENT_ROUTE_RESOLVED);
        make_qp(id);            // 路由解析完才建 QP
        rdma_conn_param cp{}; cp.responder_resources = 1; cp.initiator_depth = 1; cp.retry_count = 7; cp.rnr_retry_count = 7;
        RC_CHECK(rdma_connect(id, &cp) == 0, "rdma_connect");
        wait_event(RDMA_CM_EVENT_ESTABLISHED);
        printf("[rdma] connection established (client)\n");
    }

    // 等待並驗證一個特定的 rdma_cm 事件，型別不符就丟例外。
    void wait_event(rdma_cm_event_type want) {
        rdma_cm_event* ev = nullptr;
        RC_CHECK(rdma_get_cm_event(ec, &ev) == 0, "rdma_get_cm_event");
        if (ev->event != want) { fprintf(stderr, "expected %d got %d\n", want, ev->event); rdma_ack_cm_event(ev); throw std::runtime_error("unexpected cm event"); }
        rdma_ack_cm_event(ev);
    }

    // ---- 收發小控制訊息（用一塊已註冊的小 buffer）---------------------------
    // 預掛一個 RECV：之後對端的 SEND 或 WRITE_WITH_IMM 會消耗它並產生收完成。
    void post_recv(ibv_mr* mr, void* buf, uint32_t len) {
        ibv_sge sge{ (uint64_t)buf, len, mr->lkey };   // 描述一段本地記憶體（位址/長度/lkey）
        ibv_recv_wr wr{}; wr.wr_id = 1; wr.sg_list = &sge; wr.num_sge = 1;
        ibv_recv_wr* bad = nullptr;
        RC_CHECK(ibv_post_recv(id->qp, &wr, &bad) == 0, "ibv_post_recv");
    }
    // 送出一段小資料（SEND 語意）；SIGNALED 表示完成時要進 CQ。
    void post_send(ibv_mr* mr, void* buf, uint32_t len) {
        ibv_sge sge{ (uint64_t)buf, len, mr->lkey };
        ibv_send_wr wr{}; wr.wr_id = 2; wr.sg_list = &sge; wr.num_sge = 1;
        wr.opcode = IBV_WR_SEND; wr.send_flags = IBV_SEND_SIGNALED;
        ibv_send_wr* bad = nullptr;
        RC_CHECK(ibv_post_send(id->qp, &wr, &bad) == 0, "ibv_post_send");
    }
    // RDMA_WRITE_WITH_IMM：把本地 buf 直接寫到遠端 raddr（用對端給的 rkey），
    // 並附帶 imm 立即值。遠端會收到一筆「收完成」帶這個 imm（我們塞 frameSeq）。
    void post_write_imm(ibv_mr* mr, void* buf, uint32_t len, uint64_t raddr, uint32_t rkey, uint32_t imm) {
        ibv_sge sge{ (uint64_t)buf, len, mr->lkey };
        ibv_send_wr wr{}; wr.wr_id = 3; wr.sg_list = &sge; wr.num_sge = 1;
        wr.opcode = IBV_WR_RDMA_WRITE_WITH_IMM; wr.send_flags = IBV_SEND_SIGNALED;
        wr.imm_data = htonl(imm);                      // 立即值用網路位元序，兩端一致
        wr.wr.rdma.remote_addr = raddr;                // 遠端目標位址（server 的 GPU buffer）
        wr.wr.rdma.rkey = rkey;                        // 遠端授權金鑰
        ibv_send_wr* bad = nullptr;
        RC_CHECK(ibv_post_send(id->qp, &wr, &bad) == 0, "ibv_post_send(write_imm)");
    }
    // 阻塞輪詢「一筆」完成；狀態非 SUCCESS 即視為錯誤。回傳整個 wc（含 opcode/imm）。
    ibv_wc poll_one() {
        ibv_wc wc{};
        int n = 0;
        while ((n = ibv_poll_cq(cq, 1, &wc)) == 0) {}  // busy-poll（測試程式簡化作法）
        RC_CHECK(n > 0 && wc.status == IBV_WC_SUCCESS, "completion error");
        return wc;
    }

    // 釋放所有資源（順序：QP → CQ → PD → id → 監聽 id → 事件通道）。
    void close() {
        if (id)  { rdma_destroy_qp(id); }
        if (cq)  ibv_destroy_cq(cq);
        if (pd)  ibv_dealloc_pd(pd);
        if (id)  rdma_destroy_id(id);
        if (lid) rdma_destroy_id(lid);
        if (ec)  rdma_destroy_event_channel(ec);
    }
};
