//+------------------------------------------------------------------+
//| BarPusher.mq5 — hands CLOSED bars + heartbeats to the signal     |
//| service on the same server, and (TradeAgent.mqh, 2.x) executes   |
//| the service's orders behind hard locks: only with AllowTrading,  |
//| only for the logins in TradeLogins, only its own Magic. With the |
//| default inputs it never trades. No network code.                 |
//|                                                                  |
//| Transport: every payload (JSON as in docs/protocol.md §1-2) is   |
//| written as a file into MQL5\Files\<OutDir>. That folder is a     |
//| link to /var/spool/signal-mt5, which the service reads           |
//| (app/spool.py). Files are written as .tmp and then renamed, so   |
//| the service never sees a half-written file.                      |
//| Why not WebRequest: MT5 only allows URLs entered by hand in      |
//| Tools > Options, and a start config resets that list.            |
//|                                                                  |
//| Attach to any one chart; it handles all symbols itself.          |
//+------------------------------------------------------------------+
#property copyright "Lorant"
#property version   "2.00"
#property strict

input string OutDir          = "signal";                 // subfolder of MQL5\Files (the spool link)
input string SymbolList      = "GER40.cash,UK100.cash,US100.cash,US30.cash";
input int    BackfillM5      = 2000;                     // bars sent on first start
input int    BackfillH1      = 500;
input int    BackfillD1      = 250;
input int    PollSeconds     = 5;
input int    HeartbeatSec    = 60;

#define EA_VERSION  "2.00"
#define MAX_BATCH   500

ENUM_TIMEFRAMES g_tfs[3]      = {PERIOD_M5, PERIOD_H1, PERIOD_D1};
string          g_tfNames[3]  = {"M5", "H1", "D1"};
string          g_symbols[];
datetime        g_lastSent[];   // [symbolIndex * 3 + tfIndex], server time of last bar acknowledged
datetime        g_lastHeartbeat = 0;
long            g_seq = 0;      // makes file names unique within one run

#include "TradeAgent.mqh"

//+------------------------------------------------------------------+
int OnInit()
{
   CloseOlderDuplicateCharts();

   if(!FolderCreate(OutDir))   // true if it exists already (it is a link to the spool dir)
   {
      PrintFormat("BarPusher: cannot use folder MQL5\\Files\\%s, error %d", OutDir, GetLastError());
      return INIT_FAILED;
   }

   int n = StringSplit(SymbolList, ',', g_symbols);
   if(n <= 0)
   {
      Print("BarPusher: SymbolList is empty.");
      return INIT_PARAMETERS_INCORRECT;
   }
   for(int i = 0; i < n; i++)
   {
      StringTrimLeft(g_symbols[i]);
      StringTrimRight(g_symbols[i]);
      if(!SymbolSelect(g_symbols[i], true))
         PrintFormat("BarPusher: cannot select %s — check the name in Market Watch.", g_symbols[i]);
   }

   ArrayResize(g_lastSent, n * 3);
   ArrayInitialize(g_lastSent, 0);   // 0 = backfill on first pass; the server upserts, so resends are harmless

   TradeAgentInit();
   EventSetTimer(PollSeconds);
   PrintFormat("BarPusher %s started for %d symbols, writing to MQL5\\Files\\%s", EA_VERSION, n, OutDir);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTick() {}   // not used; everything runs on the timer

//+------------------------------------------------------------------+
//| The start config opens a new chart with this EA on every start,  |
//| and MT5 also restores the previous one. Keep only the newest     |
//| (highest chart id) chart of this symbol/period; the older one    |
//| with its EA instance is closed. Never closes other symbols.      |
//+------------------------------------------------------------------+
void CloseOlderDuplicateCharts()
{
   long me = ChartID();
   long id = ChartFirst();
   while(id >= 0)
   {
      long next = ChartNext(id);
      if(id < me && ChartSymbol(id) == _Symbol && ChartPeriod(id) == _Period)
         ChartClose(id);
      id = next;
   }
}

//+------------------------------------------------------------------+
void OnTimer()
{
   for(int s = 0; s < ArraySize(g_symbols); s++)
      for(int t = 0; t < 3; t++)
         PushNewBars(s, t);

   if(TimeLocal() - g_lastHeartbeat >= HeartbeatSec)
   {
      if(SendHeartbeat())
         g_lastHeartbeat = TimeLocal();
   }

   TradeAgentTick();
}

//+------------------------------------------------------------------+
//| Offset of server time from UTC, rounded to 15 min                |
//+------------------------------------------------------------------+
long ServerUtcOffsetSec()
{
   long raw = (long)(TimeTradeServer() - TimeGMT());
   return (long)MathRound(raw / 900.0) * 900;
}

//+------------------------------------------------------------------+
void PushNewBars(const int s, const int t)
{
   string          sym = g_symbols[s];
   ENUM_TIMEFRAMES tf  = g_tfs[t];
   int             key = s * 3 + t;

   datetime lastClosed = iTime(sym, tf, 1);       // open time of the most recent CLOSED bar
   if(lastClosed == 0 || lastClosed <= g_lastSent[key])
      return;                                     // nothing new, or history not loaded yet

   MqlRates rates[];
   ArraySetAsSeries(rates, false);                // oldest first
   int got;
   if(g_lastSent[key] == 0)
   {
      int backfill = (t == 0 ? BackfillM5 : (t == 1 ? BackfillH1 : BackfillD1));
      got = CopyRates(sym, tf, 1, backfill, rates);   // start at shift 1 → closed bars only
   }
   else
      got = CopyRates(sym, tf, g_lastSent[key] + 1, lastClosed, rates);

   if(got <= 0)
      return;                                     // retry on the next timer tick

   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);

   for(int start = 0; start < got; start += MAX_BATCH)
   {
      int end = MathMin(start + MAX_BATCH, got);
      string json = BuildBarsJson(sym, g_tfNames[t], digits, rates, start, end);
      if(!WriteJson("bars", json))
         return;                                  // keep g_lastSent; resend from here next time
      g_lastSent[key] = rates[end - 1].time;
   }
}

//+------------------------------------------------------------------+
string BuildBarsJson(const string sym, const string tfName, const int digits,
                     const MqlRates &rates[], const int from, const int to)
{
   string j = "{\"schema\":1,\"source\":\"mt5\"";
   j += ",\"account_login\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN));
   j += ",\"server\":\"" + JsonEscape(AccountInfoString(ACCOUNT_SERVER)) + "\"";
   j += ",\"symbol\":\"" + JsonEscape(sym) + "\"";
   j += ",\"timeframe\":\"" + tfName + "\"";
   j += ",\"digits\":" + IntegerToString(digits);
   j += ",\"server_utc_offset_sec\":" + IntegerToString(ServerUtcOffsetSec());
   j += ",\"bars\":[";
   for(int i = from; i < to; i++)
   {
      if(i > from) j += ",";
      j += "{\"t\":"  + IntegerToString((long)rates[i].time);
      j += ",\"o\":"  + DoubleToString(rates[i].open,  digits);
      j += ",\"h\":"  + DoubleToString(rates[i].high,  digits);
      j += ",\"l\":"  + DoubleToString(rates[i].low,   digits);
      j += ",\"c\":"  + DoubleToString(rates[i].close, digits);
      j += ",\"tv\":" + IntegerToString(rates[i].tick_volume);
      j += ",\"sp\":" + IntegerToString(rates[i].spread);
      j += "}";
   }
   j += "]}";
   return j;
}

//+------------------------------------------------------------------+
bool SendHeartbeat()
{
   string j = "{\"schema\":1";
   j += ",\"ea_version\":\"" + EA_VERSION + "\"";
   j += ",\"account_login\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN));
   j += ",\"server\":\"" + JsonEscape(AccountInfoString(ACCOUNT_SERVER)) + "\"";
   j += ",\"company\":\"" + JsonEscape(AccountInfoString(ACCOUNT_COMPANY)) + "\"";
   j += ",\"balance\":" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2);
   j += ",\"equity\":" + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2);
   j += ",\"connected\":" + (TerminalInfoInteger(TERMINAL_CONNECTED) ? "true" : "false");
   j += ",\"trade_allowed\":" + (TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) ? "true" : "false");
   // Position summary of the whole account (manual and EA trades) for /status.
   int    posCount = PositionsTotal();
   double floating = 0.0;
   for(int i = 0; i < posCount; i++)
   {
      if(PositionGetTicket(i) > 0)
         floating += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
   }
   j += ",\"positions\":" + IntegerToString(posCount);
   j += ",\"floating_pl\":" + DoubleToString(floating, 2);
   j += ",\"currency\":\"" + JsonEscape(AccountInfoString(ACCOUNT_CURRENCY)) + "\"";
   j += ",\"time_server\":" + IntegerToString((long)TimeTradeServer());
   j += ",\"server_utc_offset_sec\":" + IntegerToString(ServerUtcOffsetSec());
   j += "}";
   return WriteJson("hb", j);
}

//+------------------------------------------------------------------+
//| Write one payload as <OutDir>\<kind>_<gmt>_<usec>_<n>.json       |
//| (written as .tmp, then renamed). false = try again next time.    |
//+------------------------------------------------------------------+
bool WriteJson(const string kind, const string json)
{
   g_seq++;
   string base = StringFormat("%s_%I64d_%06I64d_%06I64d", kind, (long)TimeGMT(),
                              (long)(GetMicrosecondCount() % 1000000), g_seq % 1000000);
   string tmp  = OutDir + "\\" + base + ".tmp";
   string fin  = OutDir + "\\" + base + ".json";

   uchar data[];
   int len = StringToCharArray(json, data, 0, WHOLE_ARRAY, CP_UTF8) - 1;   // without the \0
   ResetLastError();
   int h = FileOpen(tmp, FILE_WRITE | FILE_BIN);
   if(h == INVALID_HANDLE)
   {
      PrintFormat("BarPusher: cannot create %s, error %d", tmp, GetLastError());
      return false;
   }
   uint written = FileWriteArray(h, data, 0, len);
   FileClose(h);
   if((int)written != len)
   {
      PrintFormat("BarPusher: short write on %s (%d of %d bytes)", tmp, written, len);
      FileDelete(tmp);
      return false;
   }
   if(!FileMove(tmp, 0, fin, FILE_REWRITE))
   {
      PrintFormat("BarPusher: cannot rename %s, error %d", tmp, GetLastError());
      FileDelete(tmp);
      return false;
   }
   return true;
}

//+------------------------------------------------------------------+
string JsonEscape(string s)
{
   StringReplace(s, "\\", "\\\\");
   StringReplace(s, "\"", "\\\"");
   return s;
}
//+------------------------------------------------------------------+
