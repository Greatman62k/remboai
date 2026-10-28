"""Speech recognition for RemboAI: Meta's open-source Omnilingual ASR (1600+ languages, including Swahili) on a Modal GPU.

Deploy:   pip install modal && modal setup
          modal secret create rembo-asr ASR_TOKEN=$(openssl rand -hex 24)
          modal deploy deploy/modal_asr.py
          -> prints the web URL; put  ASR_URL=<url>/transcribe  and  ASR_TOKEN=<the same token>  in .env
Try it:   modal run deploy/modal_asr.py --audio some_voice_note.ogg --lang swh_Latn
Model:    ASR_MODEL=omniASR_CTC_1B_v2 modal deploy deploy/modal_asr.py   (faster, no language hint) or the default omniASR_LLM_1B_v2.

The GPU container sleeps after 5 minutes without traffic (you pay only while it runs), so the first voice note after a quiet
spell waits for a cold start. Voice notes are processed in memory and never stored."""
import os

import modal

MODEL = os.environ.get("ASR_MODEL", "omniASR_LLM_1B_v2")
MAX_BYTES = 2_000_000

app = modal.App("rembo-asr")
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "libsndfile1")
    .pip_install("omnilingual-asr", "fastapi[standard]")
    .env({"ASR_MODEL": MODEL})
)
weights = modal.Volume.from_name("rembo-asr-weights", create_if_missing=True)  # model files are downloaded once, then reused


@app.cls(gpu="L4", image=image, volumes={"/root/.cache/fairseq2": weights}, secrets=[modal.Secret.from_name("rembo-asr")],
         scaledown_window=300, timeout=300)
class ASR:
    @modal.enter()
    def load(self):
        from omnilingual_asr.models.inference.pipeline import ASRInferencePipeline

        self.pipeline = ASRInferencePipeline(model_card=os.environ["ASR_MODEL"])
        weights.commit()

    @modal.method()
    def transcribe_bytes(self, audio: bytes, lang: str | None = None) -> str:
        """Any audio WhatsApp sends (OGG/Opus voice notes) -> 16 kHz mono WAV -> text. Longer than 40 seconds is cut off."""
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            src, wav = f"{d}/in", f"{d}/in.wav"
            with open(src, "wb") as f:
                f.write(audio)
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", src, "-ac", "1", "-ar", "16000", "-t", "40", wav], check=True, timeout=30)
            # CTC models transcribe whatever they hear; the LLM models can be told the language, which helps with Swahili and Sheng.
            kwargs = {"lang": [lang]} if lang and "CTC" not in os.environ["ASR_MODEL"] else {}
            return self.pipeline.transcribe([wav], batch_size=1, **kwargs)[0]

    @modal.asgi_app()
    def web(self):
        import hmac
        import re

        from fastapi import FastAPI, Header, HTTPException, Request
        from fastapi.concurrency import run_in_threadpool

        api = FastAPI()
        token = os.environ["ASR_TOKEN"]

        @api.get("/health")
        def health():
            return {"ok": True, "model": os.environ["ASR_MODEL"]}

        @api.post("/transcribe")
        async def transcribe(request: Request, lang: str | None = None, authorization: str = Header("")):
            if not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
                raise HTTPException(401)
            if lang is not None and not re.fullmatch(r"[a-z]{3}_[A-Z][a-z]{3}", lang):
                raise HTTPException(422, "lang looks like swh_Latn")
            audio = await request.body()
            if not 0 < len(audio) <= MAX_BYTES:
                raise HTTPException(413)
            try:
                text = await run_in_threadpool(self.transcribe_bytes.local, audio, lang)
            except Exception as e:  # an unreadable file is the caller's problem, not an outage
                raise HTTPException(422, f"could not read audio: {e.__class__.__name__}")
            return {"text": text, "model": os.environ["ASR_MODEL"]}

        return api


@app.local_entrypoint()
def check(audio: str, lang: str = ""):
    with open(audio, "rb") as f:
        print(ASR().transcribe_bytes.remote(f.read(), lang or None))
