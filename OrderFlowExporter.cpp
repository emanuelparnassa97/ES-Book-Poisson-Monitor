// ============================================================
//  OrderFlowExporter.cpp  —  Sierra Chart ACSIL Study
//  Order Flow exporter for Poisson + NegBinom analysis
// ============================================================

#include "sierrachart.h"

SCDLLName("OrderFlow Poisson Exporter")

#include <fstream>
#include <string>
#include <deque>
#include <cmath>

#define DEFAULT_OUTPUT_PATH "C:\\SierraChart\\Data\\orderflow_export.csv"

// ============================================================
// struct לשמירת trade בזיכרון
// ============================================================
struct TradeRecord {
    double  TimeDouble;  // SCDateTime כ-double (ימים מאז 1899)
    float   Price;
    int     Volume;
    int     Side;        // 1=ASK lift, -1=BID hit
};

// ============================================================
// כתיבת header ל-CSV
// ============================================================
static void WriteCSVHeader(const char* path) {
    std::ofstream f(path, std::ios::trunc);
    if (f.is_open()) {
        f << "Timestamp,Price,Volume,Side,SideLabel,"
          << "WindowCount,WindowAvgSize,LambdaBaseline,LambdaCurrent\n";
        f.close();
    }
}

// ============================================================
// חישוב λ: מרקטים לדקה בחלון נתון
// nowDouble ו-TimeDouble הם SCDateTime.GetAsDouble() — ימים
// ============================================================
static double CalcLambda(const std::deque<TradeRecord>& trades,
                         double nowDouble,
                         int windowSeconds) {
    int count = 0;
    double windowDays = (double)windowSeconds / 86400.0;
    for (size_t i = 0; i < trades.size(); i++) {
        double diffDays = nowDouble - trades[i].TimeDouble;
        if (diffDays >= 0.0 && diffDays <= windowDays) count++;
    }
    return (double)count / ((double)windowSeconds / 60.0);
}

// ============================================================
// הפונקציה הראשית
// ============================================================
SCSFExport scsf_OrderFlowPoissonExporter(SCStudyInterfaceRef sc) {

    if (sc.SetDefaults) {
        sc.GraphName        = "OrderFlow Poisson Exporter";
        sc.StudyDescription = "Exports tick-by-tick order flow to CSV";
        sc.AutoLoop         = 0;
        sc.GraphRegion      = 1;
        sc.UpdateAlways     = 1;

        sc.Input[0].Name = "Baseline Window (minutes)";
        sc.Input[0].SetInt(3);
        sc.Input[0].SetIntLimits(1, 60);

        sc.Input[1].Name = "Short Window (seconds)";
        sc.Input[1].SetInt(30);
        sc.Input[1].SetIntLimits(5, 300);

        sc.Input[2].Name = "Alert Multiplier";
        sc.Input[2].SetFloat(2.0f);

        sc.Input[3].Name = "Min Trade Size";
        sc.Input[3].SetInt(1);

        return;
    }

    // ---- persistent variables ----
    int& lastSeq     = sc.GetPersistentInt(1);
    int& headerWrote = sc.GetPersistentInt(2);

    std::deque<TradeRecord>* pTrades =
        reinterpret_cast<std::deque<TradeRecord>*>(sc.GetPersistentPointer(1));
    if (pTrades == nullptr) {
        pTrades = new std::deque<TradeRecord>();
        sc.SetPersistentPointer(1, pTrades);
    }

    // ---- פרמטרים ----
    int   baselineSeconds = sc.Input[0].GetInt() * 60;
    int   shortSeconds    = sc.Input[1].GetInt();
    float alertMult       = sc.Input[2].GetFloat();
    int   minSize         = sc.Input[3].GetInt();

    // ---- header ----
    if (headerWrote == 0) {
        WriteCSVHeader(DEFAULT_OUTPUT_PATH);
        headerWrote = 1;
    }

    // ---- Time & Sales ----
    c_SCTimeAndSalesArray tas;
    sc.GetTimeAndSales(tas);
    if (tas.Size() == 0) return;

    // זמן נוכחי כ-double (ימים)
    double nowDouble = sc.CurrentSystemDateTime.GetAsDouble();

    // ---- ניקוי trades ישנים (מעל 15 דקות) ----
    double maxAgeDays = 15.0 / 1440.0; // 15 דקות / 1440 דקות ביום
    while (!pTrades->empty()) {
        double ageDays = nowDouble - pTrades->front().TimeDouble;
        if (ageDays > maxAgeDays) pTrades->pop_front();
        else break;
    }

    // ---- פתיחת CSV ----
    std::ofstream csvFile(DEFAULT_OUTPUT_PATH, std::ios::app);
    if (!csvFile.is_open()) return;

    // ---- עיבוד trades חדשים ----
    for (int i = 0; i < tas.Size(); i++) {
        const s_TimeAndSales& r = tas[i];

        if (r.Type == SC_TS_BIDASKVALUES) continue;
        if (r.Sequence <= (unsigned int)lastSeq) continue;
        if ((int)r.Volume < minSize) continue;

        int side = 0;
        const char* sideLabel = "";

        if (r.Type == SC_TS_ASK) {
            side = 1;
            sideLabel = "ASK";
        } else if (r.Type == SC_TS_BID) {
            side = -1;
            sideLabel = "BID";
        } else {
            continue;
        }

        // שמירה בזיכרון
        TradeRecord tr;
        tr.TimeDouble = r.DateTime.GetAsDouble();
        tr.Price      = r.Price;
        tr.Volume     = (int)r.Volume;
        tr.Side       = side;
        pTrades->push_back(tr);

        // חישוב λ
        double lambdaBase    = CalcLambda(*pTrades, nowDouble, baselineSeconds);
        double lambdaCurrent = CalcLambda(*pTrades, nowDouble, shortSeconds);

        // ספירה וממוצע בחלון הקצר
        int    windowCount   = 0;
        double windowSizeSum = 0.0;
        double shortDays     = (double)shortSeconds / 86400.0;
        for (size_t j = 0; j < pTrades->size(); j++) {
            double diffDays = nowDouble - (*pTrades)[j].TimeDouble;
            if (diffDays >= 0.0 && diffDays <= shortDays) {
                windowCount++;
                windowSizeSum += (*pTrades)[j].Volume;
            }
        }
        double avgSize = windowCount > 0 ? windowSizeSum / windowCount : 0.0;

        // Alert
        if (lambdaBase > 0.0 && lambdaCurrent > lambdaBase * (double)alertMult) {
            SCString msg;
            msg.Format("SPIKE! Current=%.1f vs Base=%.1f", lambdaCurrent, lambdaBase);
            sc.AddMessageToLog(msg, 1);
        }

        // Timestamp
        int year, month, day, hour, min, sec;
        r.DateTime.GetDateTimeYMDHMS(year, month, day, hour, min, sec);
        SCString ts;
        ts.Format("%04d-%02d-%02d %02d:%02d:%02d",
                  year, month, day, hour, min, sec);

        // כתיבה ל-CSV
        csvFile << ts.GetChars() << ","
                << tr.Price      << ","
                << tr.Volume     << ","
                << side          << ","
                << sideLabel     << ","
                << windowCount   << ","
                << avgSize       << ","
                << lambdaBase    << ","
                << lambdaCurrent << "\n";

        lastSeq = (int)r.Sequence;
    }

    csvFile.close();
}
