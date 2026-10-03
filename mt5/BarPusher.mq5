//+------------------------------------------------------------------+
//| BarPusher.mq5 — phase 1: pushes CLOSED bars + heartbeats to the  |
//| signal service on the same server. It contains NO trading code.  |
//|                                                                  |
//| Setup: Tools > Options > Expert Advisors > "Allow WebRequest for |
//| listed URL" → add the ServerUrl below (http://127.0.0.1:8000).  |
//| Attach to any one chart; it handles all symbols itself.          |
//+------------------------------------------------------------------+
#property copyright "Lorant"
#property version   "1.00"
#property strict

input string ServerUrl       = "http://127.0.0.1:8000";
input string IngestToken     = "";                       // same as INGEST_TOKEN in .env
input string SymbolList      = "GER40.cash,UK100.cash,US100.cash,US30.cash";
input int    BackfillM5      = 2000;                     // bars sent on first start
input int    BackfillH1      = 500;
input int    BackfillD1      = 250;
input int    PollSeconds     = 5;
input int    HeartbeatSec    = 60;
input int    HttpTimeoutMs   = 5000;

#define EA_VERSION  "1.00"
#define MAX_BATCH   500

ENUM_TIMEFRAMES g_tfs[3]      = {PERIOD_M5, PERIOD_H1, PERIOD_D1};
string          g_tfNames[3]  = {"M5", "H1", "D1"};
string          g_symbols[];
datetime        g_lastSent[];   // [symbolIndex * 3 + tfIndex], server time of last bar acknowledged
datetime        g_lastHeartbeat = 0;

//+------------------------------------------------------------------+
int OnInit()
{
   if(IngestToken == "")
   {
      Print("BarPusher: IngestToken is empty — set it in the inputs.");
      return INIT_PARAMETERS_INCORRECT;
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

   EventSetTimer(PollSeconds);
   PrintFormat("BarPusher %s started for %d symbols → %s", EA_VERSION, n, ServerUrl);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTick() {}   // not used; everything runs on the timer

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
      if(!PostJson("/v1/bars", json))
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
   // Read-only position summary for Telegram status. No order functions are used anywhere.
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
   return PostJson("/v1/heartbeat", j);
}

//+------------------------------------------------------------------+
bool PostJson(const string path, const string json)
{
   char   body[];
   char   result[];
   string respHeaders;

   int len = StringToCharArray(json, body, 0, WHOLE_ARRAY, CP_UTF8);
   if(len > 0)
      ArrayResize(body, len - 1);   // drop the trailing \0

   string headers = "Content-Type: application/json\r\n"
                    "Authorization: Bearer " + IngestToken + "\r\n";

   ResetLastError();
   int code = WebRequest("POST", ServerUrl + path, headers, HttpTimeoutMs, body, result, respHeaders);
   if(code == -1)
   {
      int err = GetLastError();
      if(err == 4014)
         PrintFormat("BarPusher: WebRequest not allowed for %s — add it under Tools > Options > Expert Advisors.", ServerUrl);
      else
         PrintFormat("BarPusher: WebRequest %s failed, error %d", path, err);
      return false;
   }
   if(code != 200)
   {
      PrintFormat("BarPusher: %s → HTTP %d: %s", path, code, CharArrayToString(result, 0, MathMin(ArraySize(result), 300)));
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
