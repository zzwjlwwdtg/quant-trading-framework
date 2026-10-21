"""Real ephemeral HTTP, fixed fixtures, never a production service or model call."""
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import webui
import cn_decisions.store


class DecisionRoute(unittest.TestCase):
    def test_read_only_route_catalog_and_invalid_inputs(self):
        fixture={"version":1,"scope":"CN_A_FROZEN_RESEARCH_ONLY","status":"NO_RECORD", "symbol":"600000.SH","horizon_trading_days":5,"result":None}
        server=ThreadingHTTPServer(("127.0.0.1",0),webui.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            base=f"http://127.0.0.1:{server.server_port}"
            with patch.object(cn_decisions.store,"read",return_value=fixture) as read:
                with urllib.request.urlopen(base+"/api/bot/decision?symbol=600000.SH&horizon=5",timeout=3) as response:
                    self.assertEqual(json.load(response),fixture)
                read.assert_called_once_with("600000.SH",5)
                for suffix in ("symbol=600000.SH&horizon=5&url=bad","symbol=600000.SH&horizon=1&horizon=5"):
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(base+"/api/bot/decision?"+suffix,timeout=3)
                    self.assertEqual(error.exception.code,400)
                self.assertEqual(read.call_count,1)
            with urllib.request.urlopen(base+"/api/bot",timeout=3) as response:
                catalog=json.load(response)
            self.assertEqual([x["class"] for x in catalog["endpoints"] if x["path"]=="/api/bot/decision"],["read"])
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)


if __name__=="__main__":
    unittest.main()
