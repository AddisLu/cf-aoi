// =============================================================================
// rdma_common.h（IP 端）— librdmacm + ibverbs RC 連線樣板（server 側）
// -----------------------------------------------------------------------------
// 與 grab/src/rdma_common.h 同源，保持 wire format（MrInfoEx）一致。
// 兩端同步更新，不搞版本協商。
//
// ⚠️  GB10（DGX Spark）RDMA 接收必須用 cudaHostAlloc(Portable|Mapped)：
//   GB10 NVLink-C2C SoC 的 GPU Bus ID（0000000F:01:00.0）非標準 PCIe 空間，
//   modprobe nvidia_peermem 回 EINVAL（PCIe P2P 拓樸正確拒絕）。
//   正確做法：cudaHostAlloc → ibv_reg_mr → GPU 透過 NVLink-C2C (~900GB/s) 讀寫。
//   RcConn::reg() 接受任意 void*（cudaHostAlloc pinned 或普通 malloc），
//   不限定 cudaMalloc device pointer，不依賴 nvidia_peermem。
//   (2026-06-11 實機驗證，見 docs/verification/verification_report_20260611.md §五問題1)
// =============================================================================
#pragma once
#include <rdma/rdma_cma.h>
#include <infiniband/verbs.h>
#include <netdb.h>
#include <poll.h>
#include <cstring>
#include <cstdio>
#include <cstdlib>
#include <stdexcept>
#include <string>

#define RC_CHECK(x, msg) do { if (!(x)) { perror(msg); throw std::runtime_error(msg); } } while (0)

// 舊版 single-slot 握手訊息（Step 2 相容用，grab/rdma_sender.cpp 不再發送此結構）
struct MrInfo { uint64_t addr; uint32_t rkey; uint32_t len; uint32_t crc; };

// Step 3：N-slot ring buffer 握手擴充（grab/IP 兩端同步更新，不搞版本協商）。
// Grab 收到此結構後，每幀寫入位置：
//   slot_id  = frame_seq % n_slots
//   write_addr = addr + (uint64_t)slot_id * slot_size
// IP 端 post_recv N 個 = N 個初始 credit；每 memcpy 完成後補一個 post_recv = credit++。
// 背壓：credit 耗盡 → Grab WRITE_WITH_IMM → RNR（rnr_retry_count=7=∞）→
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
    rdma_event_channel* ec  = nullptr;
    rdma_cm_id*         id  = nullptr;
    rdma_cm_id*         lid = nullptr;
    ibv_pd*             pd  = nullptr;
    ibv_cq*             cq  = nullptr;
    ibv_comp_channel*   cc  = nullptr;

    void make_qp(rdma_cm_id* cm) {
        pd = ibv_alloc_pd(cm->verbs);                        RC_CHECK(pd, "ibv_alloc_pd");
        cq = ibv_create_cq(cm->verbs, 128, nullptr, nullptr, 0); RC_CHECK(cq, "ibv_create_cq");
        ibv_qp_init_attr qa{};
        qa.send_cq = cq; qa.recv_cq = cq;
        qa.qp_type = IBV_QPT_RC;
        qa.cap.max_send_wr = 32; qa.cap.max_recv_wr = 64;  // recv_wr 略多（N slot + 余裕）
        qa.cap.max_send_sge = 1; qa.cap.max_recv_sge = 1;
        RC_CHECK(rdma_create_qp(cm, pd, &qa) == 0, "rdma_create_qp");
    }

    // 接受任意 void*（cudaHostAlloc pinned / malloc），不依賴 nvidia_peermem
    ibv_mr* reg(void* buf, size_t len, int access) {
        ibv_mr* mr = ibv_reg_mr(pd, buf, len, access);
        RC_CHECK(mr, "ibv_reg_mr");
        return mr;
    }

    void serve(const char* bind_ip, const char* port) {
        ec = rdma_create_event_channel();  RC_CHECK(ec, "rdma_create_event_channel");
        RC_CHECK(rdma_create_id(ec, &lid, nullptr, RDMA_PS_TCP) == 0, "rdma_create_id");
        addrinfo* ai = nullptr; getaddrinfo(bind_ip, port, nullptr, &ai);
        RC_CHECK(rdma_bind_addr(lid, ai->ai_addr) == 0, "rdma_bind_addr");
        RC_CHECK(rdma_listen(lid, 1) == 0, "rdma_listen");
        printf("[rdma] listening on %s:%s\n", bind_ip ? bind_ip : "*", port);
        rdma_cm_event* ev = nullptr;
        RC_CHECK(rdma_get_cm_event(ec, &ev) == 0 && ev->event == RDMA_CM_EVENT_CONNECT_REQUEST,
                 "wait CONNECT_REQUEST");
        id = ev->id;
        make_qp(id);
        rdma_ack_cm_event(ev);
    }

    void accept_conn() {
        rdma_conn_param cp{};
        cp.responder_resources = 1;
        cp.initiator_depth     = 1;
        cp.retry_count         = 7;   // 7 = ∞（transport-level retry）
        cp.rnr_retry_count     = 7;   // 7 = ∞（RNR retry：credit 耗盡時 Grab 無限重試）
        RC_CHECK(rdma_accept(id, &cp) == 0, "rdma_accept");
        rdma_cm_event* ev = nullptr;
        RC_CHECK(rdma_get_cm_event(ec, &ev) == 0 && ev->event == RDMA_CM_EVENT_ESTABLISHED,
                 "wait ESTABLISHED");
        rdma_ack_cm_event(ev);
        printf("[rdma] connection established (server)\n");
    }

    // client-side connect（IP 端 rdma_source 不使用，保留結構完整性）
    void connect(const char* server_ip, const char* port) {
        ec = rdma_create_event_channel(); RC_CHECK(ec, "rdma_create_event_channel");
        RC_CHECK(rdma_create_id(ec, &id, nullptr, RDMA_PS_TCP) == 0, "rdma_create_id");
        addrinfo* ai = nullptr; getaddrinfo(server_ip, port, nullptr, &ai);
        RC_CHECK(rdma_resolve_addr(id, nullptr, ai->ai_addr, 2000) == 0, "rdma_resolve_addr");
        wait_event(RDMA_CM_EVENT_ADDR_RESOLVED);
        RC_CHECK(rdma_resolve_route(id, 2000) == 0, "rdma_resolve_route");
        wait_event(RDMA_CM_EVENT_ROUTE_RESOLVED);
        make_qp(id);
        rdma_conn_param cp{};
        cp.responder_resources = 1; cp.initiator_depth = 1;
        cp.retry_count = 7; cp.rnr_retry_count = 7;
        RC_CHECK(rdma_connect(id, &cp) == 0, "rdma_connect");
        wait_event(RDMA_CM_EVENT_ESTABLISHED);
    }

    void wait_event(rdma_cm_event_type want) {
        rdma_cm_event* ev = nullptr;
        RC_CHECK(rdma_get_cm_event(ec, &ev) == 0, "rdma_get_cm_event");
        if (ev->event != want) {
            fprintf(stderr, "[rdma] expected %d got %d\n", want, ev->event);
            rdma_ack_cm_event(ev);
            throw std::runtime_error("unexpected cm event");
        }
        rdma_ack_cm_event(ev);
    }

    void post_recv(ibv_mr* mr, void* buf, uint32_t len) {
        ibv_sge sge{ (uint64_t)buf, len, mr->lkey };
        ibv_recv_wr wr{}; wr.wr_id = 1; wr.sg_list = &sge; wr.num_sge = 1;
        ibv_recv_wr* bad = nullptr;
        RC_CHECK(ibv_post_recv(id->qp, &wr, &bad) == 0, "ibv_post_recv");
    }

    void post_send(ibv_mr* mr, void* buf, uint32_t len) {
        ibv_sge sge{ (uint64_t)buf, len, mr->lkey };
        ibv_send_wr wr{}; wr.wr_id = 2; wr.sg_list = &sge; wr.num_sge = 1;
        wr.opcode = IBV_WR_SEND; wr.send_flags = IBV_SEND_SIGNALED;
        ibv_send_wr* bad = nullptr;
        RC_CHECK(ibv_post_send(id->qp, &wr, &bad) == 0, "ibv_post_send");
    }

    void post_write_imm(ibv_mr* mr, void* buf, uint32_t len,
                        uint64_t raddr, uint32_t rkey, uint32_t imm) {
        ibv_sge sge{ (uint64_t)buf, len, mr->lkey };
        ibv_send_wr wr{}; wr.wr_id = 3; wr.sg_list = &sge; wr.num_sge = 1;
        wr.opcode = IBV_WR_RDMA_WRITE_WITH_IMM; wr.send_flags = IBV_SEND_SIGNALED;
        wr.imm_data = htonl(imm);
        wr.wr.rdma.remote_addr = raddr;
        wr.wr.rdma.rkey = rkey;
        ibv_send_wr* bad = nullptr;
        RC_CHECK(ibv_post_send(id->qp, &wr, &bad) == 0, "ibv_post_send(write_imm)");
    }

    // 阻塞輪詢一筆完成（busy-poll）。狀態非 SUCCESS 丟例外。
    ibv_wc poll_one() {
        ibv_wc wc{};
        int n = 0;
        while ((n = ibv_poll_cq(cq, 1, &wc)) == 0) {}
        RC_CHECK(n > 0 && wc.status == IBV_WC_SUCCESS, "completion error");
        return wc;
    }

    // 非阻塞輪詢一筆完成：有事件回傳 true，無事件回傳 false（用於 recv_thread 可中斷迴圈）。
    bool poll_one_nonblock(ibv_wc& wc) {
        int n = ibv_poll_cq(cq, 1, &wc);
        if (n == 0) return false;
        RC_CHECK(n > 0, "ibv_poll_cq error");
        if (wc.status != IBV_WC_SUCCESS) {
            throw std::runtime_error(std::string("WC error: ") +
                                     ibv_wc_status_str(wc.status));
        }
        return true;
    }

    // 非阻塞偵測對端斷線（RoCE v2 不保證 WR_FLUSH_ERR 立即出現，需輪詢 CM 事件頻道）。
    // 回傳 true = 偵測到 DISCONNECTED 或裝置移除；false = 無事件或不確定。
    // 在 recv_thread 的 no-event 分支呼叫，避免忙等。
    bool check_cm_disconnect() {
        if (!ec) return false;
        struct pollfd pfd{};
        pfd.fd = ec->fd;
        pfd.events = POLLIN;
        if (::poll(&pfd, 1, 0) <= 0) return false;  // 非阻塞：無事件立即返回
        rdma_cm_event* ev = nullptr;
        if (rdma_get_cm_event(ec, &ev) != 0) return false;
        bool disc = (ev->event == RDMA_CM_EVENT_DISCONNECTED ||
                     ev->event == RDMA_CM_EVENT_DEVICE_REMOVAL);
        rdma_ack_cm_event(ev);
        return disc;
    }

    void close() {
        if (id)  rdma_destroy_qp(id);
        if (cq)  ibv_destroy_cq(cq);
        if (pd)  ibv_dealloc_pd(pd);
        if (id)  rdma_destroy_id(id);
        if (lid) rdma_destroy_id(lid);
        if (ec)  rdma_destroy_event_channel(ec);
        id = nullptr; lid = nullptr; cq = nullptr; pd = nullptr; ec = nullptr;
    }
};
