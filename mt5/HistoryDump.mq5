//+------------------------------------------------------------------+
//| HistoryDump.mq5 — one-off export of price history for backtests. |
//| A SCRIPT (runs once, then ends). No trading code, no network.    |
//|                                                                  |
//| For each symbol and timeframe it writes all CLOSED bars the      |
//| broker provides to MQL5\Files\<OutDir>\<symbol>_<tf>.csv:        |
//|   t_server,o,h,l,c,tv,sp   (t = bar open, raw MT5 server epoch)  |
//| and a summary to MQL5\Files\<OutDir>\done.txt at the end.        |
//|                                                                  |
//| Run headless with deploy/mt5/history-dump.sh (start config with   |
//| Script=HistoryDump and ShutdownTerminal=1).                      |
//| The history depth is capped by "Max bars in chart"               |
//| (Config/common.ini [Charts] MaxBars); the dump script raises it. |
//+------------------------------------------------------------------+
#property copyright "Lorant"
#property version   "1.00"
#property script_show_inputs

input string SymbolList  = "GER40.cash,UK100.cash,US100.cash,US30.cash";
input string Timeframes  = "M1,M5,H1,D1";
input string OutDir      = "history";
input int    MaxBars     = 5000000;    // per symbol and timeframe
input int    SyncSeconds = 180;        // max wait for the server to deliver older history

ENUM_TIMEFRAMES ToTf(const string name)
{
   if(name == "M1") return PERIOD_M1;
   if(name == "M5") return PERIOD_M5;
   if(name == "H1") return PERIOD_H1;
   if(name == "D1") return PERIOD_D1;
   return PERIOD_CURRENT;
}

//+------------------------------------------------------------------+
//| Copy closed bars (from shift 1). MT5 downloads older history on  |
//| request, so ask again until the count stops growing.             |
//+------------------------------------------------------------------+
int CopyAll(const string sym, const ENUM_TIMEFRAMES tf, MqlRates &rates[])
{
   int best = 0;
   int stable = 0;
   uint started = GetTickCount();
   while(GetTickCount() - started < (uint)SyncSeconds * 1000)
   {
      ArrayFree(rates);
      int got = CopyRates(sym, tf, 1, MaxBars, rates);
      if(got > best)
      {
         best = got;
         stable = 0;
      }
      else if(got > 0 && ++stable >= 3 && SeriesInfoInteger(sym, tf, SERIES_SYNCHRONIZED))
         break;                                   // no growth three times in a row: done
      Sleep(2000);
   }
   ArrayFree(rates);
   return CopyRates(sym, tf, 1, MaxBars, rates);
}

void OnStart()
{
   string syms[], tfs[];
   int ns = StringSplit(SymbolList, ',', syms);
   int nt = StringSplit(Timeframes, ',', tfs);
   FolderCreate(OutDir);
   string summary = "";

   for(int i = 0; i < ns; i++)
   {
      string sym = syms[i];
      StringTrimLeft(sym);
      StringTrimRight(sym);
      if(!SymbolSelect(sym, true))
      {
         summary += StringFormat("%s: cannot select symbol\r\n", sym);
         continue;
      }
      int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      for(int j = 0; j < nt; j++)
      {
         string tfName = tfs[j];
         StringTrimLeft(tfName);
         StringTrimRight(tfName);
         ENUM_TIMEFRAMES tf = ToTf(tfName);
         if(tf == PERIOD_CURRENT)
            continue;

         MqlRates rates[];
         ArraySetAsSeries(rates, false);          // oldest first
         int got = CopyAll(sym, tf, rates);
         if(got <= 0)
         {
            summary += StringFormat("%s %s: no data (error %d)\r\n", sym, tfName, GetLastError());
            continue;
         }

         string file = OutDir + "\\" + sym + "_" + tfName + ".csv";
         int h = FileOpen(file, FILE_WRITE | FILE_TXT | FILE_ANSI);
         if(h == INVALID_HANDLE)
         {
            summary += StringFormat("%s %s: cannot write %s (error %d)\r\n", sym, tfName, file, GetLastError());
            continue;
         }
         FileWriteString(h, StringFormat("# symbol=%s tf=%s digits=%d\r\n", sym, tfName, digits));
         FileWriteString(h, "t_server,o,h,l,c,tv,sp\r\n");
         for(int k = 0; k < got; k++)
            FileWriteString(h, StringFormat("%I64d,%s,%s,%s,%s,%I64d,%d\r\n",
                            (long)rates[k].time,
                            DoubleToString(rates[k].open, digits), DoubleToString(rates[k].high, digits),
                            DoubleToString(rates[k].low, digits), DoubleToString(rates[k].close, digits),
                            rates[k].tick_volume, rates[k].spread));
         FileClose(h);
         summary += StringFormat("%s %s: %d bars, %s .. %s (server time)\r\n", sym, tfName, got,
                                 TimeToString(rates[0].time), TimeToString(rates[got - 1].time));
      }
   }

   int d = FileOpen(OutDir + "\\done.txt", FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(d != INVALID_HANDLE)
   {
      FileWriteString(d, summary);
      FileClose(d);
   }
   Print("HistoryDump finished:\r\n", summary);
}
//+------------------------------------------------------------------+
