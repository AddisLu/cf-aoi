#pragma once
// pylon 參數存取 stub（見 PylonIncludes.h 檔頭的適用範圍說明）
#include "PylonIncludes.h"

namespace Pylon {

class CIntegerParameter {
public:
    CIntegerParameter(GenApi::INodeMap&, const char* name) : name_(name) {}
    bool    TrySetValue(int64_t v) { PylonStub::ints[name_] = v; return true; }
    void    SetValue(int64_t v) {
        if (v > GetMax()) throw GenericException();
        PylonStub::ints[name_] = v;
    }
    int64_t GetValue() const {
        auto& m = PylonStub::ints;
        if (name_ == "PayloadSize" && m.count("Width") && m.count("Height"))
            return m.at("Width") * m.at("Height");
        auto it = m.find(name_);
        return it == m.end() ? 0 : it->second;
    }
    int64_t GetMax() const {
        auto it = PylonStub::int_max.find(name_);
        return it == PylonStub::int_max.end() ? (int64_t)1 << 30 : it->second;
    }
private:
    std::string name_;
};

class CFloatParameter {
public:
    CFloatParameter(GenApi::INodeMap&, const char*) {}
    bool   TrySetValue(double) { return true; }
    void   SetValue(double)    {}
    double GetValue() const    { return 0.0; }
    // cam_pylon open() 以 GetMax() 取「不設限」的行速率上限（--line-rate max）
    double GetMax() const      { return 1e9; }
    double GetMin() const      { return 0.0; }
};

class CEnumParameter {
public:
    CEnumParameter(GenApi::INodeMap&, const char*) {}
    bool     TrySetValue(const char*) { return true; }
    void     SetValue(const char*)    {}
    String_t GetValue() const         { return String_t("Off"); }
};

} // namespace Pylon
