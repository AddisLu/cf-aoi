#pragma once
// pylon 參數存取 stub（見 PylonIncludes.h 檔頭的適用範圍說明）
#include "PylonIncludes.h"

namespace Pylon {

class CIntegerParameter {
public:
    CIntegerParameter(GenApi::INodeMap&, const char*) {}
    bool    TrySetValue(int64_t) { return true; }
    void    SetValue(int64_t)    {}
    int64_t GetValue() const     { return 0; }
};

class CFloatParameter {
public:
    CFloatParameter(GenApi::INodeMap&, const char*) {}
    bool   TrySetValue(double) { return true; }
    void   SetValue(double)    {}
    double GetValue() const    { return 0.0; }
};

class CEnumParameter {
public:
    CEnumParameter(GenApi::INodeMap&, const char*) {}
    bool     TrySetValue(const char*) { return true; }
    void     SetValue(const char*)    {}
    String_t GetValue() const         { return String_t("Off"); }
};

} // namespace Pylon
