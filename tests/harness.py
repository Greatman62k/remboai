"""Test harness: the real app and real clients, with Meta's Graph API and the language model faked at the HTTP layer."""
import hashlib
import hmac
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx

from app import db
from app.config import Settings
from app.ctx import Ctx
from app.llm import Extractor
from app.main import app
from app.mpesa import Daraja
from app.vision import StyleMatcher
from app.voice import Transcriber
from app.whatsapp import WhatsApp

NBO = ZoneInfo("Africa/Nairobi")
PID, SECRET = "PID1", "s3cret"


class Outside:
    """Records every WhatsApp send. The model answers from a script; set .down to make endpoints fail."""

    def __init__(self):
        self.sent: list[dict] = []
        self.script: dict[str, dict] = {}
        self.down: set[str] = set()      # hostnames that return 503
        self.calls: list[str] = []       # hostnames of model calls
        self.asr: list[dict] = []        # speech-recognition requests received
        self.asr_text = "nataka fade kesho mchana"
        self.asr_down = False
        self.media_gone = False          # Meta no longer has the voice note
        self.media_size = 20_000
        self.media_mime = "audio/ogg; codecs=opus"
        self.vision: list[dict] = []     # image requests the model received
        self.vision_answer = {"service": "Fade", "style": "low skin fade", "confidence": 0.9}
        self.stk: list[dict] = []        # STK pushes Daraja received
        self.stk_fails = False           # make Daraja refuse pushes
        self.queries: dict[str, dict] = {}   # checkout id -> status query answer (absent: "still processing")

    def handler(self, req: httpx.Request) -> httpx.Response:
        host = req.url.host
        if host == "graph.facebook.com" and req.method == "GET":        # media lookup: where is voice note X?
            if self.media_gone:
                return httpx.Response(404, json={"error": {"code": 100}})
            return httpx.Response(200, json={"url": f"https://lookaside.fbsbx.com/m/{req.url.path.rsplit('/', 1)[1]}", "mime_type": self.media_mime,
                                             "file_size": self.media_size})
        if host == "lookaside.fbsbx.com":
            return httpx.Response(200, content=b"OggS-fake-voice-note", headers={"content-type": "audio/ogg"})
        if host == "asr.test":
            if self.asr_down:
                return httpx.Response(503)
            self.asr.append({"auth": req.headers["authorization"], "type": req.headers["content-type"], "lang": req.url.params.get("lang"), "bytes": req.content})
            return httpx.Response(200, json={"text": self.asr_text})
        if host == "graph.facebook.com":
            self.sent.append(json.loads(req.content) | {"_auth": req.headers["authorization"]})
            return httpx.Response(200, json={"messages": [{"id": "x"}]})
        if req.url.path.endswith("/chat/completions") and isinstance(json.loads(req.content)["messages"][-1]["content"], list):
            body = json.loads(req.content)                       # a photo: content is a list of text and image parts
            image = next(p for p in body["messages"][-1]["content"] if p["type"] == "image_url")["image_url"]["url"]
            self.vision.append({"host": host, "model": body["model"], "image": image, "prompt": body["messages"][0]["content"]})
            if host in self.down:
                return httpx.Response(503)
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(self.vision_answer)}}]})
        if req.url.path.endswith("/chat/completions"):
            self.calls.append(host)
            if host in self.down:
                return httpx.Response(503, json={"error": "down"})
            body = json.loads(req.content)
            text = body["messages"][-1]["content"].split('Message: "', 1)[1].rsplit('"', 1)[0].lower()
            ans = self.script.get(text, {"intent": "other"})
            return httpx.Response(200, json={"choices": [{"message": {"content": "```json\n" + json.dumps(ans) + "\n```"}}]})
        if host == "sandbox.safaricom.co.ke":
            return self.daraja(req)
        return httpx.Response(404)

    def daraja(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth/v1/generate":
            return httpx.Response(200, json={"access_token": "AT", "expires_in": "3599"})
        if req.headers.get("authorization") != "Bearer AT":
            return httpx.Response(401, json={"errorMessage": "bad token"})
        body = json.loads(req.content)
        if req.url.path.endswith("/processrequest"):
            if self.stk_fails:
                return httpx.Response(500, json={"errorCode": "500.001.1001", "errorMessage": "Merchant does not exist"})
            self.stk.append(body)
            n = len(self.stk)
            return httpx.Response(200, json={"MerchantRequestID": f"mr{n}", "CheckoutRequestID": f"ws_CO_{n}", "ResponseCode": "0",
                                             "ResponseDescription": "Success", "CustomerMessage": "Success"})
        if req.url.path.endswith("/query"):
            ans = self.queries.get(body["CheckoutRequestID"])
            if ans is None:
                return httpx.Response(500, json={"errorCode": "500.001.1001", "errorMessage": "The transaction is being processed"})
            return httpx.Response(200, json={"ResponseCode": "0", "ResponseDescription": "ok", **ans})
        return httpx.Response(404)

    def to(self, wa_id: str) -> list[dict]:
        return [m for m in self.sent if m["to"] == wa_id]

    def bodies(self, wa_id: str) -> list[str]:
        out = []
        for m in self.to(wa_id):
            out.append(m["text"]["body"] if m["type"] == "text" else m["interactive"]["body"]["text"])
        return out


CB_SECRET = "cb-secret"


def callback(checkout_id: str, code: int = 0, receipt: str = "QAB1CD2EF3", amount: int = 200, phone: str = "254711000001") -> dict:
    """What Safaricom posts to /mpesa/callback/<secret> when a prompt ends."""
    cb = {"MerchantRequestID": "mr", "CheckoutRequestID": checkout_id, "ResultCode": code,
          "ResultDesc": "The service request is processed successfully." if code == 0 else "Request cancelled by user"}
    if code == 0:
        cb["CallbackMetadata"] = {"Item": [{"Name": "Amount", "Value": amount}, {"Name": "MpesaReceiptNumber", "Value": receipt},
                                           {"Name": "TransactionDate", "Value": 20261027190000}, {"Name": "PhoneNumber", "Value": int(phone)}]}
    return {"Body": {"stkCallback": cb}}


def open_day(offset_min: int = 1) -> date:
    """A future day the salon is open (not Sunday)."""
    d = datetime.now(NBO).date() + timedelta(days=offset_min)
    while d.weekday() == 6:
        d += timedelta(days=1)
    return d


def payload(wa_id: str, text: str = "", reply_id: str | None = None, mid: str = "m1") -> dict:
    if reply_id:
        m = {"from": wa_id, "id": mid, "type": "interactive", "interactive": {"button_reply": {"id": reply_id, "title": "t"}}}
    else:
        m = {"from": wa_id, "id": mid, "type": "text", "text": {"body": text}}
    return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": PID}, "contacts": [{"wa_id": wa_id, "profile": {"name": "Cust"}}], "messages": [m]}}]}]}


async def post(c: httpx.AsyncClient, body: dict) -> httpx.Response:
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return await c.post("/webhook", content=raw, headers={"x-hub-signature-256": sig, "content-type": "application/json"})


async def build(dsn: str, till: bool = True, **over):
    pool = await db.make_pool(dsn)
    await pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await db.migrate(pool)
    salon = await pool.fetchval("INSERT INTO salons (name, wa_phone_number_id) VALUES ('Zuri Cuts', $1) RETURNING id::text", PID)
    brian = await pool.fetchval("INSERT INTO staff (salon_id, name) VALUES ($1::uuid,'Brian') RETURNING id::text", salon)
    fade = await pool.fetchval("INSERT INTO services (salon_id,name,aliases,duration_min,price_kes) VALUES ($1::uuid,'Fade','{}',45,500) RETURNING id::text", salon)
    silk = await pool.fetchval("INSERT INTO services (salon_id,name,aliases,duration_min,price_kes) VALUES ($1::uuid,'Silk press','{silk}',90,2500) RETURNING id::text", salon)
    for s in (fade, silk):
        await pool.execute("INSERT INTO staff_services VALUES ($1::uuid,$2::uuid)", brian, s)
    if till:
        await pool.execute("INSERT INTO salon_payment (salon_id, kind, shortcode, party_b) VALUES ($1::uuid,'till','174379','5012345')", salon)
    out = Outside()
    conf = dict(database_url=dsn, wa_app_secret=SECRET, wa_verify_token="v", wa_access_token="TOK",
                llm_base_url="https://primary.test/v1", llm_api_key="k", llm_model="muse-spark-1.3",
                mpesa_consumer_key="ck", mpesa_consumer_secret="cs", mpesa_passkey="pk", mpesa_callback_secret=CB_SECRET,
                public_base_url="https://rembo.test", asr_url="https://asr.test/transcribe", asr_token="asrtok")
    conf.update(over)
    settings = Settings(_env_file=None, **conf)
    http = httpx.AsyncClient(transport=httpx.MockTransport(out.handler))
    wa = WhatsApp(http, settings.graph_version)
    app.state.settings, app.state.pool, app.state.wa = settings, pool, wa
    llm = Extractor(http, settings)
    app.state.ctx = Ctx(settings, pool, wa, llm, Daraja(http, settings), Transcriber(http, settings), StyleMatcher(http, settings, llm.endpoints))
    return pool, out, salon


def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
