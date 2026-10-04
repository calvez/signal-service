//+------------------------------------------------------------------+
//| TradeAgent.mqh — order execution for BarPusher 2.x.              |
//| See docs/execution.md.                                           |
//|                                                                  |
//| The signal service writes ORDER FILES (key=value text) into      |
//| MQL5\Files\<OutDir>\<OrdersDir>\*.ord. This module executes them |
//| and reports every result back through the spool (kind "exec"),   |
//| plus a snapshot of its own positions and orders (kind "pos").    |
//|                                                                  |
//| HARD LOCKS, enforced here whatever the service sends:            |
//|  - AllowTrading input must be true (master switch)               |
//|  - the logged-in account must be listed in TradeLogins, and the  |
//|    order must name that same login                               |
//|  - only symbols from SymbolList; only positions/orders with this |
//|    EA's Magic are ever touched (never manual trades)             |
//|  - orders older than their 'expires' time are ignored            |
//|  - open + pending lots per symbol <= MaxLotsPerSymbol            |
//|  - own daily loss stop: equity down DailyStopPct % of            |
//|    InitialBalance from the FTMO day start (midnight Prague)      |
//|    -> close everything, no new orders until the next FTMO day    |
//|  - flat time per symbol (sent with the entry): positions and     |
//|    orders of that symbol are closed then, even if the service    |
//|    is down                                                       |
//+------------------------------------------------------------------+
#include <Trade\Trade.mqh>

input bool   AllowTrading     = false;    // master switch for order execution
input string TradeLogins      = "";       // account logins allowed to trade, comma separated
input long   Magic            = 26100;    // marks this EA's positions and orders
input double InitialBalance   = 160000;   // FTMO initial balance (base of the daily stop)
input double DailyStopPct     = 4.0;      // own daily loss stop in % (FTMO's limit is 5)
input double MaxLotsPerSymbol = 100;      // hard cap: open + pending lots per symbol
input string OrdersDir        = "orders"; // subfolder of OutDir with the order files

CTrade   g_trade;
string   g_flatSym[];
long     g_flatAt[];                      // UTC epoch: be flat in this symbol from then on
long     g_dayKey          = -1;
double   g_dayStartBalance = 0;
bool     g_dailyStopped    = false;
string   g_lastPos         = "";
datetime g_lastPosSent     = 0;

//+------------------------------------------------------------------+
//| FTMO day = calendar day in Prague (CET/CEST, EU DST rules)       |
//+------------------------------------------------------------------+
datetime LastSundayUtc(const int year, const int month)
{
   MqlDateTime d;
   ZeroMemory(d);
   d.year = (month == 12) ? year + 1 : year;
   d.mon  = (month == 12) ? 1 : month + 1;
   d.day  = 1;
   datetime lastDay = StructToTime(d) - 86400;
   MqlDateTime l;
   TimeToStruct(lastDay, l);
   return lastDay - l.day_of_week * 86400;
}

long PragueDayKey(const datetime gmt)
{
   MqlDateTime d;
   TimeToStruct(gmt, d);
   datetime summerFrom = LastSundayUtc(d.year, 3) + 3600;    // 01:00 UTC
   datetime summerTo   = LastSundayUtc(d.year, 10) + 3600;
   long offset = (gmt >= summerFrom && gmt < summerTo) ? 7200 : 3600;
   return ((long)gmt + offset) / 86400;
}

//+------------------------------------------------------------------+
bool LoginAllowed()
{
   string logins[];
   int n = StringSplit(TradeLogins, ',', logins);
   long me = AccountInfoInteger(ACCOUNT_LOGIN);
   for(int i = 0; i < n; i++)
   {
      StringTrimLeft(logins[i]);
      StringTrimRight(logins[i]);
      if(StringLen(logins[i]) > 0 && StringToInteger(logins[i]) == me)
         return true;
   }
   return false;
}

bool SymbolAllowed(const string sym)
{
   for(int i = 0; i < ArraySize(g_symbols); i++)
      if(g_symbols[i] == sym)
         return true;
   return false;
}

bool TradingLocked(string &why)
{
   if(!AllowTrading)                                { why = "AllowTrading is off";                 return true; }
   if(!LoginAllowed())                              { why = "this login is not in TradeLogins";    return true; }
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)) { why = "algo trading is off in the terminal"; return true; }
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED))           { why = "algo trading is off for this EA";     return true; }
   if(g_dailyStopped)                               { why = "own daily loss stop reached";         return true; }
   return false;
}

//+------------------------------------------------------------------+
//| key=value order files                                            |
//+------------------------------------------------------------------+
int ReadKv(const string path, string &keys[], string &vals[])
{
   ArrayResize(keys, 0);
   ArrayResize(vals, 0);
   int h = FileOpen(path, FILE_READ | FILE_TXT | FILE_ANSI);
   if(h == INVALID_HANDLE)
      return 0;
   while(!FileIsEnding(h))
   {
      string line = FileReadString(h);
      int eq = StringFind(line, "=");
      if(eq <= 0)
         continue;
      int n = ArraySize(keys);
      ArrayResize(keys, n + 1);
      ArrayResize(vals, n + 1);
      keys[n] = StringSubstr(line, 0, eq);
      vals[n] = StringSubstr(line, eq + 1);
      StringTrimRight(vals[n]);
   }
   FileClose(h);
   return ArraySize(keys);
}

string Kv(const string &keys[], const string &vals[], const string key)
{
   for(int i = 0; i < ArraySize(keys); i++)
      if(keys[i] == key)
         return vals[i];
   return "";
}

//+------------------------------------------------------------------+
//| Results back to the service (spool kind "exec")                  |
//+------------------------------------------------------------------+
void Report(const string id, const string action, const string sym, const bool ok,
            const long retcode, const long ticket, const double lots, const double price,
            const string message)
{
   string j = "{\"schema\":1";
   j += ",\"order_id\":\"" + JsonEscape(id) + "\"";
   j += ",\"action\":\"" + JsonEscape(action) + "\"";
   j += ",\"symbol\":\"" + JsonEscape(sym) + "\"";
   j += ",\"ok\":" + (ok ? "true" : "false");
   j += ",\"retcode\":" + IntegerToString(retcode);
   j += ",\"ticket\":" + IntegerToString(ticket);
   j += ",\"lots\":" + DoubleToString(lots, 2);
   j += ",\"price\":" + DoubleToString(price, 5);
   j += ",\"message\":\"" + JsonEscape(message) + "\"";
   j += ",\"account_login\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN));
   j += ",\"time_utc\":" + IntegerToString((long)TimeGMT());
   j += "}";
   WriteJson("exec", j);
   PrintFormat("TradeAgent: %s %s %s -> %s (%d) %s", id, action, sym, ok ? "ok" : "REFUSED", retcode, message);
}

//+------------------------------------------------------------------+
double SymbolLots(const string sym)
{
   double lots = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
      if(PositionGetTicket(i) > 0 && PositionGetInteger(POSITION_MAGIC) == Magic
         && PositionGetString(POSITION_SYMBOL) == sym)
         lots += PositionGetDouble(POSITION_VOLUME);
   for(int i = OrdersTotal() - 1; i >= 0; i--)
      if(OrderGetTicket(i) > 0 && OrderGetInteger(ORDER_MAGIC) == Magic
         && OrderGetString(ORDER_SYMBOL) == sym)
         lots += OrderGetDouble(ORDER_VOLUME_CURRENT);
   return lots;
}

void SetFlatTime(const string sym, const long flatAt)
{
   for(int i = 0; i < ArraySize(g_flatSym); i++)
      if(g_flatSym[i] == sym)
      {
         g_flatAt[i] = flatAt;
         return;
      }
   int n = ArraySize(g_flatSym);
   ArrayResize(g_flatSym, n + 1);
   ArrayResize(g_flatAt, n + 1);
   g_flatSym[n] = sym;
   g_flatAt[n] = flatAt;
}

//+------------------------------------------------------------------+
//| Entry or add: a stop order (buy stop above / sell stop below)    |
//| lots = given, or sized from risk_money and the stop distance     |
//+------------------------------------------------------------------+
void PlaceStop(const string id, const string action, const string sym, const string dir,
               double price, double sl, double lots, const double riskMoney, const long flatAt,
               const string comment)
{
   int    digits    = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double tickSize  = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
   double tickValue = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
   double step      = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   double vmin      = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double vmax      = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   bool   isLong    = (dir == "long");

   if(tickSize <= 0 || tickValue <= 0 || step <= 0)
   {
      Report(id, action, sym, false, 0, 0, 0, price, "symbol has no tick size/value");
      return;
   }
   price = NormalizeDouble(MathRound(price / tickSize) * tickSize, digits);
   sl    = NormalizeDouble(MathRound(sl / tickSize) * tickSize, digits);
   if((isLong && sl >= price) || (!isLong && sl <= price))
   {
      Report(id, action, sym, false, 0, 0, 0, price, "stop on the wrong side of the entry");
      return;
   }
   if(lots <= 0)
   {
      double lossPerLot = MathAbs(price - sl) / tickSize * tickValue;
      lots = (lossPerLot > 0) ? riskMoney / lossPerLot : 0;
   }
   lots = MathFloor(lots / step + 1e-9) * step;
   if(lots < vmin)
   {
      Report(id, action, sym, false, 0, 0, lots, price, "size below the minimum lot");
      return;
   }
   lots = MathMin(lots, vmax);
   if(SymbolLots(sym) + lots > MaxLotsPerSymbol)
   {
      Report(id, action, sym, false, 0, 0, lots, price, "MaxLotsPerSymbol would be exceeded");
      return;
   }
   double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
   double bid = SymbolInfoDouble(sym, SYMBOL_BID);
   if((isLong && ask >= price) || (!isLong && bid <= price))
   {
      Report(id, action, sym, false, 0, 0, lots, price, "market already beyond the entry price");
      return;
   }

   g_trade.SetExpertMagicNumber(Magic);
   bool ok = isLong
             ? g_trade.BuyStop(lots, price, sym, sl, 0, ORDER_TIME_GTC, 0, comment)
             : g_trade.SellStop(lots, price, sym, sl, 0, ORDER_TIME_GTC, 0, comment);
   if(ok && flatAt > 0)
      SetFlatTime(sym, flatAt);
   Report(id, action, sym, ok, (long)g_trade.ResultRetcode(), (long)g_trade.ResultOrder(), lots, price,
          g_trade.ResultRetcodeDescription());
}

//+------------------------------------------------------------------+
//| New common stop for all positions and pending orders of a symbol |
//+------------------------------------------------------------------+
void ModifySl(const string id, const string sym, double sl)
{
   int    digits   = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double tickSize = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
   if(tickSize > 0)
      sl = NormalizeDouble(MathRound(sl / tickSize) * tickSize, digits);
   int done = 0, failed = 0;
   g_trade.SetExpertMagicNumber(Magic);
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong t = PositionGetTicket(i);
      if(t == 0 || PositionGetInteger(POSITION_MAGIC) != Magic || PositionGetString(POSITION_SYMBOL) != sym)
         continue;
      if(MathAbs(PositionGetDouble(POSITION_SL) - sl) < tickSize / 2)
         continue;
      if(g_trade.PositionModify(t, sl, PositionGetDouble(POSITION_TP))) done++; else failed++;
   }
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong t = OrderGetTicket(i);
      if(t == 0 || OrderGetInteger(ORDER_MAGIC) != Magic || OrderGetString(ORDER_SYMBOL) != sym)
         continue;
      if(g_trade.OrderModify(t, OrderGetDouble(ORDER_PRICE_OPEN), sl, OrderGetDouble(ORDER_TP),
                             ORDER_TIME_GTC, 0)) done++; else failed++;
   }
   Report(id, "modify_sl", sym, failed == 0, (long)g_trade.ResultRetcode(), 0, 0, sl,
          StringFormat("%d modified, %d failed", done, failed));
}

//+------------------------------------------------------------------+
//| Close positions and/or delete pending orders ("*" = all symbols) |
//+------------------------------------------------------------------+
void CloseSymbol(const string id, const string sym, const bool positionsToo, const string why)
{
   int closed = 0, deleted = 0, failed = 0;
   g_trade.SetExpertMagicNumber(Magic);
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong t = OrderGetTicket(i);
      if(t == 0 || OrderGetInteger(ORDER_MAGIC) != Magic)
         continue;
      if(sym != "*" && OrderGetString(ORDER_SYMBOL) != sym)
         continue;
      if(g_trade.OrderDelete(t)) deleted++; else failed++;
   }
   if(positionsToo)
      for(int i = PositionsTotal() - 1; i >= 0; i--)
      {
         ulong t = PositionGetTicket(i);
         if(t == 0 || PositionGetInteger(POSITION_MAGIC) != Magic)
            continue;
         if(sym != "*" && PositionGetString(POSITION_SYMBOL) != sym)
            continue;
         if(g_trade.PositionClose(t)) closed++; else failed++;
      }
   Report(id, positionsToo ? "close_all" : "cancel_pending", sym, failed == 0,
          (long)g_trade.ResultRetcode(), 0, 0, 0,
          StringFormat("%s: %d closed, %d orders deleted, %d failed", why, closed, deleted, failed));
}

//+------------------------------------------------------------------+
void ExecuteOrderFile(const string path)
{
   string keys[], vals[];
   ReadKv(path, keys, vals);
   string id     = Kv(keys, vals, "id");
   string action = Kv(keys, vals, "action");
   string sym    = Kv(keys, vals, "symbol");
   string why;

   // close_all / cancel_pending reduce risk: allowed even when new orders are locked
   bool reducing = (action == "close_all" || action == "cancel_pending");
   if(!reducing && TradingLocked(why))
   {
      Report(id, action, sym, false, 0, 0, 0, 0, why);
      return;
   }
   if(StringToInteger(Kv(keys, vals, "login")) != AccountInfoInteger(ACCOUNT_LOGIN))
   {
      Report(id, action, sym, false, 0, 0, 0, 0, "order is for another login");
      return;
   }
   if(StringToInteger(Kv(keys, vals, "expires")) < (long)TimeGMT())
   {
      Report(id, action, sym, false, 0, 0, 0, 0, "order expired before it was read");
      return;
   }
   if(sym != "*" && !SymbolAllowed(sym))
   {
      Report(id, action, sym, false, 0, 0, 0, 0, "symbol not in SymbolList");
      return;
   }

   if(action == "open" || action == "add")
      PlaceStop(id, action, sym, Kv(keys, vals, "direction"),
                StringToDouble(Kv(keys, vals, "price")), StringToDouble(Kv(keys, vals, "sl")),
                StringToDouble(Kv(keys, vals, "lots")), StringToDouble(Kv(keys, vals, "risk_money")),
                StringToInteger(Kv(keys, vals, "flat_at")), Kv(keys, vals, "comment"));
   else if(action == "modify_sl")
      ModifySl(id, sym, StringToDouble(Kv(keys, vals, "sl")));
   else if(action == "close_all")
      CloseSymbol(id, sym, true, Kv(keys, vals, "reason"));
   else if(action == "cancel_pending")
      CloseSymbol(id, sym, false, Kv(keys, vals, "reason"));
   else
      Report(id, action, sym, false, 0, 0, 0, 0, "unknown action");
}

void SortNames(string &names[])
{
   for(int i = 1; i < ArraySize(names); i++)
   {
      string v = names[i];
      int j = i - 1;
      while(j >= 0 && StringCompare(names[j], v) > 0)
      {
         names[j + 1] = names[j];
         j--;
      }
      names[j + 1] = v;
   }
}

void ProcessOrders()
{
   string dir = OutDir + "\\" + OrdersDir + "\\";
   string names[], f;
   long h = FileFindFirst(dir + "*.ord", f);
   if(h == INVALID_HANDLE)
      return;
   do
   {
      if(StringFind(f, ".ord") == StringLen(f) - 4)
      {
         int n = ArraySize(names);
         ArrayResize(names, n + 1);
         names[n] = f;
      }
   }
   while(FileFindNext(h, f));
   FileFindClose(h);
   SortNames(names);                               // oldest first (names start with a time)
   for(int i = 0; i < ArraySize(names); i++)
   {
      ExecuteOrderFile(dir + names[i]);
      FileDelete(dir + names[i]);
   }
}

//+------------------------------------------------------------------+
//| Guards that work even when the service is down                   |
//+------------------------------------------------------------------+
void DailyGuard()
{
   long key = PragueDayKey(TimeGMT());
   if(key != g_dayKey)
   {
      g_dayKey = key;
      g_dayStartBalance = AccountInfoDouble(ACCOUNT_BALANCE);
      g_dailyStopped = false;
   }
   if(g_dailyStopped || !LoginAllowed())
      return;
   double lost = g_dayStartBalance - AccountInfoDouble(ACCOUNT_EQUITY);
   if(lost >= DailyStopPct / 100.0 * InitialBalance)
   {
      g_dailyStopped = true;
      CloseSymbol("guard-daily", "*", true, StringFormat("own daily stop: %.2f lost today", lost));
   }
}

void FlatGuard()
{
   long now = (long)TimeGMT();
   for(int i = 0; i < ArraySize(g_flatSym); i++)
      if(g_flatAt[i] > 0 && now >= g_flatAt[i])
      {
         if(SymbolLots(g_flatSym[i]) > 0)
            CloseSymbol("guard-flat", g_flatSym[i], true, "flat time reached");
         g_flatAt[i] = 0;
      }
}

//+------------------------------------------------------------------+
//| Snapshot of this EA's positions and orders (spool kind "pos")    |
//+------------------------------------------------------------------+
void ReportPositions()
{
   string p = "", o = "";
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong t = PositionGetTicket(i);
      if(t == 0 || PositionGetInteger(POSITION_MAGIC) != Magic)
         continue;
      if(p != "") p += ",";
      p += StringFormat("{\"ticket\":%I64u,\"symbol\":\"%s\",\"type\":\"%s\",\"volume\":%.2f,"
                        "\"price_open\":%.5f,\"sl\":%.5f,\"profit\":%.2f,\"comment\":\"%s\"}",
                        t, PositionGetString(POSITION_SYMBOL),
                        PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? "buy" : "sell",
                        PositionGetDouble(POSITION_VOLUME), PositionGetDouble(POSITION_PRICE_OPEN),
                        PositionGetDouble(POSITION_SL), PositionGetDouble(POSITION_PROFIT),
                        JsonEscape(PositionGetString(POSITION_COMMENT)));
   }
   for(int i = 0; i < OrdersTotal(); i++)
   {
      ulong t = OrderGetTicket(i);
      if(t == 0 || OrderGetInteger(ORDER_MAGIC) != Magic)
         continue;
      if(o != "") o += ",";
      o += StringFormat("{\"ticket\":%I64u,\"symbol\":\"%s\",\"type\":\"%s\",\"volume\":%.2f,"
                        "\"price\":%.5f,\"sl\":%.5f,\"comment\":\"%s\"}",
                        t, OrderGetString(ORDER_SYMBOL),
                        OrderGetInteger(ORDER_TYPE) == ORDER_TYPE_BUY_STOP ? "buy_stop" : "sell_stop",
                        OrderGetDouble(ORDER_VOLUME_CURRENT), OrderGetDouble(ORDER_PRICE_OPEN),
                        OrderGetDouble(ORDER_SL), JsonEscape(OrderGetString(ORDER_COMMENT)));
   }
   string why = "";
   bool locked = TradingLocked(why);
   string body = "\"positions\":[" + p + "],\"orders\":[" + o + "]"
                 + ",\"locked\":" + (locked ? "true" : "false")
                 + ",\"lock_reason\":\"" + JsonEscape(why) + "\""
                 + ",\"daily_stopped\":" + (g_dailyStopped ? "true" : "false");
   // send on every change, otherwise once a minute
   if(body == g_lastPos && TimeLocal() - g_lastPosSent < 60)
      return;
   string j = "{\"schema\":1,\"account_login\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN))
              + ",\"time_utc\":" + IntegerToString((long)TimeGMT()) + "," + body + "}";
   if(WriteJson("pos", j))
   {
      g_lastPos = body;
      g_lastPosSent = TimeLocal();
   }
}

//+------------------------------------------------------------------+
void TradeAgentInit()
{
   FolderCreate(OutDir + "\\" + OrdersDir);
   string why = "";
   bool locked = TradingLocked(why);
   PrintFormat("TradeAgent: trading %s%s (magic %I64d, daily stop %.1f%% of %.0f)",
               locked ? "LOCKED: " : "enabled", locked ? why : "", Magic, DailyStopPct, InitialBalance);
}

void TradeAgentTick()
{
   DailyGuard();
   FlatGuard();
   ProcessOrders();      // also when locked: every order gets an answer
   ReportPositions();
}
//+------------------------------------------------------------------+
