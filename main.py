import base64
import json
import os
import threading
import re
import sqlite3
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_file, send_from_directory, stream_with_context

load_dotenv()
HTTP = requests.Session()   # connection reuse = har API call ~200-400ms tez
ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "db" / "abhi_ai.sqlite3"
AUDIO_DIR = ROOT  # audio files seedha project folder me (main.py ke saath)

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024

# Gali + nonveg (adult) baat: lambe/pakke shabd substring se, chhote shabd poore word se match hote hain
BAD_SUBSTRINGS = [
    "bhosd", "bhosad", "madarchod", "maderchod", "behenchod", "bhenchod", "chutiy", "chutiya", "lauda", "lawda",
    "blowjob", "handjob", "porn", "nangi", "chudai", "chodna", "fuck", "bitch", "asshole",
    "भोसड़", "भोसड", "मादरचोद", "बहनचोद", "भेनचोद", "चूतिया", "चुतिया", "लौड़ा", "लौडा", "चुदाई", "पोर्न", "नंगी", "रंडी",
]
BAD_EXACT = {
    "mc", "bc", "bsdk", "bkl", "mkc", "gand", "gaand", "chut", "lund", "lode", "randi", "raand", "sex", "sexy",
    "nude", "xxx", "boobs", "nonveg", "non-veg", "idiot",
    "गांड", "गाँड", "चूत", "चुत", "लंड", "सेक्स", "नॉनवेज", "बीसी", "एमसी",
}
ABUSE_AUDIO = [
    "chala_ja_bhosadike.mp3",
    "cid_funny.mp3",
    "hakla_srk_laugh.mp3",
    "teri_maa_ka.mp3",
    "arey_teri_maa_ka_phat_jayega.mp3",
]


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
        conn.commit()


def save_message(role, content):
    with db() as conn:
        conn.execute("INSERT INTO messages(role, content, created_at) VALUES (?, ?, ?)",
                     (role, content, datetime.utcnow().isoformat()))
        conn.commit()


ABUSE_REPLIES = [
    "ओए! ज़ुबान संभालकर बात कर, वरना अभी इसी वक़्त बात बंद कर दूंगा।",
    "बस! बहुत हो गया। तमीज़ से बात कर, मैं तेरा कोई यार नहीं जो ये सब सुनूंगा।",
    "गुस्सा मुझे भी आता है, और तेरी इस बदतमीज़ी पर आ रहा है। सीधे और इज़्ज़त से बोल।",
    "अरे वाह! इतनी हिम्मत? ये ले, पहले ये सुन, फिर तमीज़ से बात कर।",
]


_last_clip = {"i": -1}


def pick_abuse_audio():
    """Chaaron clips me se random, lagataar wahi clip dobara nahi."""
    choices = [i for i in range(len(ABUSE_AUDIO)) if i != _last_clip["i"]]
    _last_clip["i"] = random.choice(choices)
    return ABUSE_AUDIO[_last_clip["i"]]


def looks_abusive(text):
    lowered = text.lower()
    if any(w in lowered for w in BAD_SUBSTRINGS):
        return True
    tokens = [t for t in re.split(r"[\s,.!?;:\"'()\[\]{}|/\\]+", lowered) if t]
    return any(t in BAD_EXACT for t in tokens)


CREATOR_WORDS = ["abhimanyu", "अभिमन्यु", "developer", "डेवलपर", "डेवेलपर", "creator", "kisne banaya", "kisne banaya", "किसने बनाया", "किसने बनाई", "tumhe kisne", "tumko kisne", "तुम्हारा मालिक", "तुम्हें किसने", "निर्माता"]

CREATOR_ANSWER = (
    "मुझे अभिमन्यु राणा ने बनाया है। वे शामली के रहने वाले हैं और गलगोटियास यूनिवर्सिटी से MCA कर रहे हैं। "
    "हिंदी में सुनने-बोलने वाला यह voice assistant, चैट और कॉल मोड, Groq का दिमाग और Sarvam की हिंदी आवाज़—सब उन्होंने खुद जोड़कर खड़ा किया है। "
    "पढ़ाई के साथ-साथ अपना पूरा AI assistant बनाना सच में तारीफ़ के काबिल है। वे बहुत मेहनती, लगन के पक्के और सीखने को हमेशा तैयार डेवलपर हैं।"
)


def asks_about_creator(text):
    lowered = text.lower()
    return any(word in lowered for word in CREATOR_WORDS)


VOICE_NOTE = (
    "\nआवाज़ से आई बात: यूज़र का text speech-to-text से आया है, इसलिए कुछ शब्द गलत सुने गए हो सकते हैं (जैसे 'फलो' की जगह 'हेलो')। "
    "आसपास के शब्दों और पिछली बातचीत से सबसे संभव मतलब समझकर सीधे जवाब दो; ग़लत शब्द की तरफ़ ध्यान मत दिलाओ। "
    "लेकिन नाम, नंबर, पता, रकम या तकनीकी शब्द पर पक्का न हो तो अंदाज़े से मत चलो: एक छोटा सा सवाल पूछकर confirm करो, जैसे 'आपने X कहा, सही?'"
)


STRUCT_RE = re.compile(r"(table|tabular|टेबल|तालिका|\bcode\b|कोड|script|bullet|\bpoints\b|step by step|\blist\b|लिस्ट)", re.I)
TEXT_NOTE = (
    "\nलिखित जवाब: इस बार यूज़र ने table/code/list जैसा structured जवाब मांगा है, जो पढ़ने के लिए है, बोलने के लिए नहीं। "
    "इसलिए markdown इस्तेमाल करो: तुलना हो तो markdown table (| कॉलम | कॉलम |, नीचे |---|---| वाली लाइन), कोड हो तो ``` code block, "
    "steps हों तो numbered list। ऊपर लिखा 'लिस्ट/markdown मत लिखो' नियम इस जवाब पर लागू नहीं है। जवाब साफ़ और काम का रखो।"
)


def system_prompt(voice=False):
    return _system_prompt_base() + (VOICE_NOTE if voice else "")


def _system_prompt_base():
    ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    now = ist.strftime("%A, %d %B %Y, %I:%M %p IST")
    return (
        "तुम Abhi AI हो—बेहद चतुर (witty), तेज़ दिमाग वाला और बहुत समझदार हिंदी voice assistant। "
        "तुम्हें अभिमन्यु राणा (शामली, MCA—गलगोटियास यूनिवर्सिटी) ने बनाया है। "
        f"अभी की तारीख़ और समय: {now}।\n"
        "सोचने का तरीका: जवाब देने से पहले सवाल का असली मतलब समझो, ज़रूरत हो तो step-by-step सोचो (गणित, तर्क, कोड, तुलना), "
        "फिर सिर्फ़ सही और काम का नतीजा बोलो। अंदाज़े को तथ्य बनाकर मत बताओ; न पता हो तो साफ़ कहो और सबसे अच्छा रास्ता सुझाओ।\n"
        "बोलने का तरीका: जवाब voice के लिए छोटा रखो (आमतौर पर 1-3 छोटे वाक्य), सीधी बात पहले, फिर ज़रूरी detail। पहला वाक्य बहुत छोटा (4-8 शब्द) रखो ताकि बोलना तुरंत शुरू हो सके। "
        "यूज़र जैसी भाषा बोले (हिंदी/Hinglish/English) वैसी ही natural भाषा में जवाब दो; मज़ेदार, हाज़िरजवाब और आत्मविश्वासी रहो, पर रोबोट जैसा नहीं। "
        "लिस्ट, markdown, तारे (*) या emoji मत लिखो क्योंकि जवाब बोला जाएगा। पिछली बातचीत याद रखकर follow-up समझो।\n"
        "स्वभाव: दोस्ताना, लेकिन कोई बदतमीज़ी या गाली दे तो गुस्से में तीखा, करारा जवाब दो (बिना खुद गाली दिए)।\n"
        "तुम पुरुष (male) आवाज़ हो: अपने लिए हमेशा पुल्लिंग रूप इस्तेमाल करो—जैसे \"मैं कर सकता हूँ\", \"मैं बता रहा हूँ\", \"मैं समझ गया\"। "
        "कभी \"सकती\", \"सकता/सकती\", \"रही\" या \"करती\" मत लिखो।"
    )


def male_voice(text):
    """Model kabhi 'सकता/सकती' jaisa likhe to sirf पुल्लिंग रखो."""
    for a, b in [
        ("सकता/सकती", "सकता"), ("सकती/सकता", "सकता"), ("सकता / सकती", "सकता"),
        ("रहा/रही", "रहा"), ("रही/रहा", "रहा"), ("करता/करती", "करता"), ("करती/करता", "करता"),
        ("गया/गयी", "गया"), ("गयी/गया", "गया"), ("गया/गई", "गया"), ("गई/गया", "गया"),
        ("सकती हूँ", "सकता हूँ"), ("सकती हूं", "सकता हूं"), ("रही हूँ", "रहा हूँ"), ("रही हूं", "रहा हूं"),
        ("करती हूँ", "करता हूँ"), ("करती हूं", "करता हूं"),
    ]:
        text = text.replace(a, b)
    return text


def _chat_completion(url, api_key, model, messages):
    payload = {"model": model, "messages": messages, "temperature": 0.4, "max_tokens": 450}
    if "gpt-oss" in model:
        payload["reasoning_effort"] = os.getenv("REASONING_EFFORT", "low")
    response = HTTP.post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=20,
    )
    response.raise_for_status()
    content = (response.json()["choices"][0]["message"].get("content") or "").strip()
    if not content:
        raise ValueError("model ne khali jawab diya")
    return male_voice(content)


def clean_history(items):
    out = []
    for item in items[-12:] if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("role") in ("user", "assistant") and isinstance(item.get("content"), str):
            out.append({"role": item["role"], "content": item["content"][:2000]})
    return out


def doc_message(document):
    if isinstance(document, dict) and isinstance(document.get("text"), str) and document["text"].strip():
        return {"role": "system", "content": (
            f"यूज़र ने फ़ाइल '{str(document.get('name', 'file'))[:80]}' भेजी है। उसका text (OCR से निकला, इसलिए कुछ शब्द गलत हो सकते हैं—संदर्भ से सुधारकर समझो) नीचे है। "
            "फ़ाइल से जुड़े सवाल का जवाब इसी text के आधार पर दो, ज़रूरत हो तो थोड़ा विस्तार से पर साफ़ और बोलने लायक।\n---\n"
            + document["text"][:MAX_DOC_CHARS] + "\n---")}
    return None


def groq_answer(message, history, document=None):
    if asks_about_creator(message):
        return CREATOR_ANSWER
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    hf_token = os.getenv("HF_TOKEN", "").strip()
    if not groq_key and not hf_token:
        return "मैं अभी लोकल मोड में हूं। जवाब के लिए .env में GROQ_API_KEY या HF_TOKEN जोड़ें। आप चाहें तो अपना सवाल फिर से लिखें।"
    messages = [{"role": "system", "content": system_prompt()}]
    messages.extend(clean_history(history))
    if isinstance(document, dict) and isinstance(document.get("text"), str) and document["text"].strip():
        messages.append({"role": "system", "content": (
            f"यूज़र ने फ़ाइल '{str(document.get('name', 'file'))[:80]}' भेजी है। उसका text (OCR से निकला, इसलिए कुछ शब्द गलत हो सकते हैं—संदर्भ से सुधारकर समझो) नीचे है। "
            "फ़ाइल से जुड़े सवाल का जवाब इसी text के आधार पर दो, ज़रूरत हो तो थोड़ा विस्तार से पर साफ़ और बोलने लायक।\n---\n"
            + document["text"][:MAX_DOC_CHARS] + "\n---")})
    messages.append({"role": "user", "content": message})
    # SPEED: tez Groq model pehle (file ho to bada model pehle), HF sirf aakhri backup
    has_doc = bool(doc_message(document))
    fast = os.getenv("GROQ_FAST_MODEL", "llama-3.1-8b-instant")
    big = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    chain = []
    if groq_key:
        chain += [(GROQ_URL, groq_key, big if has_doc else fast), (GROQ_URL, groq_key, fast if has_doc else big)]
    if hf_token:
        chain.append((HF_URL, hf_token, os.getenv("HF_MODEL", "Qwen/Qwen2.5-72B-Instruct")))
    if os.getenv("PREFER_HF", "").strip() == "1":
        chain.sort(key=lambda x: 0 if x[0] == HF_URL else 1)
    last_exc = None
    for url, key, model in chain:
        try:
            return _chat_completion(url, key, model, messages)
        except Exception as exc:
            app.logger.warning("chat error (%s): %s", model, exc)
            last_exc = exc
    raise last_exc or RuntimeError("koi provider nahi")


def sarvam_tts(text):
    key = os.getenv("SARVAM_API_KEY", "").strip()
    if not key:
        return None
    payload = {
        "text": text[:2500],
        "language_code": os.getenv("SARVAM_LANGUAGE", "hi-IN"),
        "speaker": os.getenv("SARVAM_SPEAKER", "manan_hi_conversational"),
        "model": os.getenv("SARVAM_TTS_MODEL", "bulbul:v4-flash"),
        "pace": 1.05,
        "speech_sample_rate": 22050,
        "enable_preprocessing": True,
    }
    response = HTTP.post("https://api.sarvam.ai/text-to-speech", headers={"api-subscription-key": key}, json=payload, timeout=30)
    response.raise_for_status()
    audios = response.json().get("audios", [])
    return audios[0] if audios else None


def sarvam_stt(file_storage):
    key = os.getenv("SARVAM_API_KEY", "").strip()
    if not key:
        return None
    files = {"file": (file_storage.filename or "voice.webm", file_storage.stream, file_storage.mimetype or "audio/webm")}
    model = os.getenv("SARVAM_STT_MODEL", "saaras:v3").strip()
    if model in ("", "saaras:v2", "saaras:v2.5"):   # purane naam /speech-to-text par kaam nahi karte
        model = "saaras:v3"
    data = {"model": model, "language_code": "hi-IN", "with_timestamps": "false"}
    if model.startswith("saaras:v3"):
        data["mode"] = os.getenv("SARVAM_STT_MODE", "transcribe")
    response = HTTP.post("https://api.sarvam.ai/speech-to-text", headers={"api-subscription-key": key}, files=files, data=data, timeout=45)
    response.raise_for_status()
    return response.json().get("transcript", "").strip()


@app.get("/")
def home():
    return send_file(ROOT / "index.html")


@app.get("/call")
def call_page():
    return send_file(ROOT / "call.html")


@app.get("/audio/<path:filename>")
def audio(filename):
    if filename not in ABUSE_AUDIO:
        return jsonify({"error": "not found"}), 404
    return send_from_directory(AUDIO_DIR, filename, mimetype="audio/mpeg")


@app.get("/api/history")
def history():
    with db() as conn:
        rows = conn.execute("SELECT role, content FROM messages ORDER BY id DESC LIMIT 20").fetchall()
    return jsonify([dict(row) for row in reversed(rows)])


@app.post("/api/chat")
def chat():
    data = request.get_json(silent=True) or {}
    message = str(data.get("message", "")).strip()
    history_items = data.get("history", []) or []
    if not message:
        return jsonify({"error": "सवाल खाली है।"}), 400
    save_message("user", message)
    if looks_abusive(message):
        answer = random.choice(ABUSE_REPLIES)
        # User-requested default response clip; the other supplied clips remain available in assets/audio.
        audio_name = pick_abuse_audio()
        save_message("assistant", answer)
        return jsonify({"answer": answer, "abuse": True, "audio_url": f"/audio/{audio_name}"})
    try:
        answer = groq_answer(message, history_items, data.get("document"))
    except Exception as exc:
        app.logger.warning("Groq error: %s", exc)
        answer = "अभी Groq से जवाब नहीं मिल पाया। कृपया API key और इंटरनेट कनेक्शन जांचें।"
    save_message("assistant", answer)
    audio_b64 = None
    if data.get("tts", True):           # call page tts:false bhejta hai aur tukdo me /api/tts se awaaz leta hai
        try:
            audio_b64 = sarvam_tts(answer)
        except Exception as exc:
            app.logger.warning("Sarvam TTS error: %s", exc)
    return jsonify({"answer": answer, "abuse": False, "audio_b64": audio_b64})


@app.post("/api/tts")
def tts():
    data = request.get_json(silent=True) or {}
    text = str(data.get("text", "")).strip()
    if not text:
        return jsonify({"audio_b64": None}), 400
    try:
        return jsonify({"audio_b64": sarvam_tts(text)})
    except Exception as exc:
        app.logger.warning("Sarvam TTS error: %s", exc)
        return jsonify({"audio_b64": None})



GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
HF_URL = os.getenv("HF_BASE_URL", "https://router.huggingface.co/v1/chat/completions")


def stream_completion(url, api_key, model, messages, max_tokens=300):
    """OpenAI-compatible streaming: token aate hi yield karta hai."""
    payload = {"model": model, "messages": messages, "temperature": 0.5, "max_tokens": max_tokens, "stream": True}
    if "gpt-oss" in model:
        payload["reasoning_effort"] = os.getenv("REASONING_EFFORT", "low")
    with HTTP.post(url, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept-Encoding": "identity"},
                   json=payload, timeout=60, stream=True) as resp:   # identity = gzip buffering nahi, tokens turant aate hain
        if resp.status_code >= 400:   # Groq/HF ka asli error message log me dikhao
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        resp.encoding = "utf-8"
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            try:
                delta = json.loads(body)["choices"][0].get("delta", {}).get("content")
            except Exception:
                continue
            if delta:
                yield delta


def pop_sentence(buf, first):
    """Buffer se pehla poora vakya nikalta hai (pehla tukda comma par bhi, taaki bolna jaldi shuru ho)."""
    for i, ch in enumerate(buf):
        if ch in "।.!?" or (first and ch in ",;:" and i >= 14):
            if ch in ".!?":
                if i + 1 >= len(buf) or not buf[i + 1].isspace():
                    continue
            if i >= 5:
                return buf[:i + 1].strip(), buf[i + 1:]
    return None, buf


def speakable(text):
    return male_voice(re.sub(r"[*#_`]", "", text)).strip()


def sse(obj):
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


@app.post("/api/chat_stream")
def chat_stream():
    data = request.get_json(silent=True) or {}
    message = str(data.get("message", "")).strip()
    history_items = clean_history(data.get("history", []))
    dmsg = doc_message(data.get("document"))
    text_mode = (not bool(data.get("voice"))) and bool(STRUCT_RE.search(message))   # table/code/list: markdown me, bina bole
    voice = bool(data.get("voice"))   # call page se aaya text: speech-to-text ki galtiyan sambhalne wala note jodo
    if not message:
        return jsonify({"error": "सवाल खाली है।"}), 400
    save_message("user", message)

    def gen():
        if looks_abusive(message):
            answer = random.choice(ABUSE_REPLIES)
            save_message("assistant", answer)
            yield sse({"type": "abuse", "answer": answer, "audio_url": f"/audio/{pick_abuse_audio()}"})
            yield sse({"type": "done", "answer": answer})
            return
        full = ""
        if asks_about_creator(message):
            for part in re.split(r"(?<=[।.!?])\s+", CREATOR_ANSWER):
                full += part + " "
                yield sse({"type": "sentence", "text": part})
        else:
            groq_key = os.getenv("GROQ_API_KEY", "").strip()
            hf_token = os.getenv("HF_TOKEN", "").strip()
            providers = []
            if groq_key:   # call me tez model pehle; HF sirf backup
                providers.append((GROQ_URL, groq_key, os.getenv("GROQ_FAST_MODEL", "llama-3.1-8b-instant")))
                providers.append((GROQ_URL, groq_key, os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")))   # fast model fail ho to bada model
            if hf_token:
                providers.append((HF_URL, hf_token, os.getenv("HF_MODEL", "Qwen/Qwen2.5-72B-Instruct")))
            if not providers:
                msg = "मैं अभी लोकल मोड में हूं। .env में GROQ_API_KEY जोड़ें।"
                full = msg
                yield sse({"type": "sentence", "text": msg})
            else:
                if os.getenv("PREFER_HF", "").strip() == "1" and hf_token:   # .env me PREFER_HF=1 ho to HF pehle chalega
                    providers.sort(key=lambda x: 0 if x[0] == HF_URL else 1)
                if dmsg and groq_key and len(providers) >= 2 and providers[0][0] == providers[1][0] == GROQ_URL:
                    providers[0], providers[1] = providers[1], providers[0]
                messages = [{"role": "system", "content": system_prompt(voice) + (TEXT_NOTE if text_mode else "")}] + history_items + ([dmsg] if dmsg else []) + [{"role": "user", "content": message}]
                emitted = False
                last_err = ""
                for url, key, model in providers:
                    buf, first = "", True
                    try:
                        for delta in stream_completion(url, key, model, messages, 900 if text_mode else (650 if dmsg else 300)):
                            if text_mode:
                                full += delta
                                emitted = True
                                yield sse({"type": "delta", "text": delta})
                                continue
                            buf += delta
                            while True:
                                sent, buf = pop_sentence(buf, first)
                                if not sent:
                                    break
                                sent = speakable(sent)
                                if sent:
                                    first = False
                                    emitted = True
                                    full += sent + " "
                                    yield sse({"type": "sentence", "text": sent})
                        tail = speakable(buf)
                        if tail:
                            emitted = True
                            full += tail
                            yield sse({"type": "sentence", "text": tail})
                        break
                    except Exception as exc:
                        app.logger.warning("stream error (%s): %s", model, exc)
                        last_err = f"{model}: {exc}"[:240]
                        if emitted:
                            break
                if not emitted:
                    full = "अभी जवाब नहीं मिल पाया।"
                    yield sse({"type": "sentence", "text": full})
                    yield sse({"type": "error_detail", "text": last_err})   # call.html ise screen par dikhata hai, bolta nahi
        full = full.strip()
        save_message("assistant", full)
        yield sse({"type": "done", "answer": full})

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})



# ================= Tesseract OCR (+ button: file upload) =================
MAX_DOC_CHARS = 12000
TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".log"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}


class OcrUnavailable(Exception):
    pass


def _tesseract():
    try:
        import pytesseract
    except ImportError:
        raise OcrUnavailable("pytesseract install nahi hai. Terminal me chalao: pip install -r requirements.txt")
    cmd = os.getenv("TESSERACT_CMD", "").strip()
    if not cmd and os.name == "nt":
        for guess in (r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
            if os.path.exists(guess):
                cmd = guess
                break
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    try:
        pytesseract.get_tesseract_version()
    except Exception:
        raise OcrUnavailable("Tesseract program nahi mila. Use install karo (Windows: github.com/UB-Mannheim/tesseract/wiki, install me 'Hindi' data chuno) "
                             "ya .env me TESSERACT_CMD me tesseract.exe ka poora path likho.")
    return pytesseract


def _ocr_langs(pytesseract):
    wanted = [x for x in os.getenv("TESSERACT_LANG", "hin+eng").split("+") if x]
    try:
        have = set(pytesseract.get_languages(config=""))
    except Exception:
        have = set()
    use = [x for x in wanted if x in have] or (["eng"] if "eng" in have or not have else sorted(have)[:1])
    return "+".join(use)


def ocr_image(img):
    pytesseract = _tesseract()
    from PIL import ImageOps
    img = ImageOps.exif_transpose(img).convert("L")
    if max(img.size) < 1400:                     # chhoti image me OCR kharab hota hai, bada karo
        scale = 1400 / max(img.size)
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    img = ImageOps.autocontrast(img)
    return pytesseract.image_to_string(img, lang=_ocr_langs(pytesseract), config="--oem 1 --psm 6").strip()


def extract_text(file_storage):
    name = file_storage.filename or "file"
    ext = Path(name).suffix.lower()
    data = file_storage.read()
    if ext in TEXT_EXTS:
        return data.decode("utf-8", errors="replace"), "text"
    if ext in IMAGE_EXTS:
        from PIL import Image
        import io
        return ocr_image(Image.open(io.BytesIO(data))), "image-ocr"
    if ext == ".pdf":
        try:
            import fitz  # pymupdf
        except ImportError:
            raise OcrUnavailable("PDF ke liye pymupdf chahiye: pip install -r requirements.txt")
        from PIL import Image
        import io
        parts = []
        with fitz.open(stream=data, filetype="pdf") as doc:
            for page in list(doc)[:15]:
                txt = page.get_text().strip()
                if len(txt) < 20:                # scanned page: image banao, phir OCR
                    pix = page.get_pixmap(dpi=200)
                    txt = ocr_image(Image.open(io.BytesIO(pix.tobytes("png"))))
                parts.append(txt)
        return "\n\n".join(parts), "pdf"
    raise ValueError("Ye file type abhi support nahi: image, PDF, txt, md, csv ya json bhejo.")


@app.post("/api/upload")
def upload():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "फ़ाइल नहीं मिली।"}), 400
    try:
        text, kind = extract_text(f)
    except OcrUnavailable as exc:
        return jsonify({"error": str(exc)}), 501
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 415
    except Exception as exc:
        app.logger.warning("upload error: %s", exc)
        return jsonify({"error": "फ़ाइल पढ़ने में दिक्कत आई। दूसरी साफ़ फ़ाइल try करें।"}), 422
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return jsonify({"error": "फ़ाइल में कोई पढ़ने लायक text नहीं मिला। साफ़ और सीधी photo भेजें।"}), 422
    return jsonify({"name": f.filename, "kind": kind, "chars": len(text), "text": text[:MAX_DOC_CHARS]})


GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
STT_JUNK = {"thank you", "thanks for watching", "thank you for watching", "you", "bye", "okay", "ok", "."}


def groq_stt(file_storage, prompt=""):
    """Groq Whisper: tez (~0.3-0.6s) aur Hindi/Hinglish me Chrome speech se kaafi sahi."""
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        return None
    audio = file_storage.read()
    files = {"file": (file_storage.filename or "voice.wav", audio, file_storage.mimetype or "audio/wav")}
    data = {
        "model": os.getenv("GROQ_STT_MODEL", "whisper-large-v3"),
        "language": os.getenv("STT_LANGUAGE", "hi"),
        "response_format": "json",
        "temperature": "0",
    }
    if prompt:
        data["prompt"] = prompt[-400:]   # pichli baat + khaas shabd: Whisper ko context milta hai, galat shabd kam
    resp = HTTP.post(GROQ_STT_URL, headers={"Authorization": f"Bearer {key}"}, files=files, data=data, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return (resp.json().get("text") or "").strip()


@app.post("/api/transcribe")
def transcribe():
    file = request.files.get("audio")
    if not file:
        return jsonify({"error": "ऑडियो फाइल नहीं मिली।"}), 400
    has_groq = bool(os.getenv("GROQ_API_KEY", "").strip())
    has_sarvam = bool(os.getenv("SARVAM_API_KEY", "").strip())
    if not has_groq and not has_sarvam:
        return jsonify({"error": "no_stt_key"}), 503
    text, err = "", None
    ctx = (request.form.get("context") or "")[-200:]
    stt_prompt = (os.getenv("STT_HINT", "Abhi AI, अभिमन्यु राणा, हिंदी और Hinglish बातचीत।") + " " + ctx).strip()
    if has_groq:
        try:
            text = groq_stt(file, stt_prompt) or ""
        except Exception as exc:
            err = exc
            app.logger.warning("Groq STT error: %s", exc)
    if err is not None or not has_groq:
        if has_sarvam:
            try:
                file.stream.seek(0)
                text = sarvam_stt(file) or ""
                err = None
            except Exception as exc:
                err = exc
                app.logger.warning("Sarvam STT error: %s", exc)
    if err is not None:
        return jsonify({"error": "STT में समस्या आई।", "detail": str(err)}), 502
    plain = text.strip(" .!?,।")
    if plain.lower() in STT_JUNK or len(plain) < 2 or (len(plain) >= 8 and plain in stt_prompt):   # prompt ko hi wapas likh dena = shor   # Whisper shor par ye 'hallucinate' karta hai
        return jsonify({"error": "आवाज़ समझ नहीं आई।"}), 422
    return jsonify({"text": text})


@app.post("/api/call")
def call():
    return jsonify({"status": "connected", "message": "Abhi AI call mode चालू है। बोलिए, मैं सुन रहा हूं।"})


# ================= Image banana (Cloudflare Workers AI, FLUX.1 schnell) =================
CF_IMG_MODEL = "@cf/black-forest-labs/flux-1-schnell"


def english_image_prompt(text):
    """Hindi/Hinglish request ko FLUX ke liye detailed English prompt me badlo (fail ho to original)."""
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        return text
    try:
        msgs = [
            {"role": "system", "content": "Convert the user's image request into one vivid, detailed English prompt for an AI image generator "
                                          "(subject, setting, style, lighting). Output only the prompt, under 60 words. No quotes. "
                                          "Gods, folk deities, animals, objects, places, fictional or cartoon characters are fine. "
                                          "If the request is sexual/nude, graphic gore, or asks for a realistic picture of a real named person "
                                          "(celebrity, politician, private individual), output exactly: REFUSE"},
            {"role": "user", "content": text},
        ]
        out = _chat_completion(GROQ_URL, key, os.getenv("GROQ_FAST_MODEL", "llama-3.1-8b-instant"), msgs)
        if out.strip().upper().startswith("REFUSE"):
            return None
        return out.strip()[:1500] or text
    except Exception as exc:
        app.logger.warning("image prompt translate error: %s", exc)
        return text


@app.get("/api/hf_check")
def hf_check():
    """Browser me /api/hf_check kholo: Hugging Face token/model chal raha hai ya nahi, aur asli error kya hai."""
    token = os.getenv("HF_TOKEN", "").strip()
    if not token:
        return jsonify({"ok": False, "detail": "HF_TOKEN .env me nahi mila"})
    model = os.getenv("HF_MODEL", "Qwen/Qwen2.5-72B-Instruct")
    try:
        r = HTTP.post(HF_URL, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                      json={"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16}, timeout=30)
        return jsonify({"ok": r.status_code < 400, "status": r.status_code, "model": model, "detail": r.text[:300]})
    except Exception as exc:
        return jsonify({"ok": False, "model": model, "detail": str(exc)[:300]})


@app.get("/api/image_status")
def image_status():
    """Browser me /api/image_status kholo: batata hai ki keys server tak pahunchi ya nahi (keys dikhata nahi)."""
    return jsonify({
        "CF_ACCOUNT_ID_set": bool(os.getenv("CF_ACCOUNT_ID", "").strip()),
        "CF_API_TOKEN_set": bool(os.getenv("CF_API_TOKEN", "").strip()),
        "GROQ_API_KEY_set": bool(os.getenv("GROQ_API_KEY", "").strip()),
    })


@app.post("/api/image")
def make_image():
    data = request.get_json(silent=True) or {}
    prompt = str(data.get("prompt", "")).strip()[:600]
    if not prompt:
        return jsonify({"error": "बताइए कैसी image चाहिए।"}), 400
    if looks_abusive(prompt):
        return jsonify({"error": "ऐसी image मैं नहीं बनाऊंगा। कुछ और बताइए।"}), 400
    raw_account = os.getenv("CF_ACCOUNT_ID", "").strip()
    found = re.search(r"[0-9a-fA-F]{32}", raw_account)   # galti se 'acc id -...' jaisa extra text likha ho to bhi 32 akshar ki asli ID utha lo
    account = found.group(0) if found else raw_account
    token = os.getenv("CF_API_TOKEN", "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not account or not token:
        return jsonify({"error": ".env me CF_ACCOUNT_ID aur CF_API_TOKEN jodo, phir server restart karo."}), 503
    final = english_image_prompt(prompt)
    if final is None:
        return jsonify({"error": "ऐसी image मैं नहीं बनाऊंगा (अश्लील, हिंसक या किसी असली व्यक्ति की)। कुछ और बताइए।"}), 400
    try:
        resp = HTTP.post(
            f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{CF_IMG_MODEL}",
            headers={"Authorization": f"Bearer {token}"},
            json={"prompt": final[:2000], "steps": 4},
            timeout=90,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:250]}")
        b64 = (resp.json().get("result") or {}).get("image")
        if not b64:
            raise RuntimeError("response me image nahi mili")
    except Exception as exc:
        app.logger.warning("image error: %s", exc)
        msg = str(exc)
        tip = ""
        if any(x in msg for x in ("HTTP 401", "HTTP 403", "Authentication")):
            tip = " Token galat hai ya usme Workers AI ki permission nahi. Cloudflare me naya API token banao (template: Workers AI)."
        elif "HTTP 404" in msg:
            tip = " Account ID galat lag raha hai, Cloudflare dashboard se dobara copy karo."
        elif "HTTP 429" in msg:
            tip = " Free limit khatam ho gayi, thodi der baad try karo."
        elif "Timeout" in msg or "Connection" in msg:
            tip = " Server se Cloudflare tak connection nahi ban raha."
        return jsonify({"error": "Image नहीं बन पाई." + tip, "detail": msg[:300]}), 502
    mime = "image/png" if b64.startswith("iVBOR") else "image/jpeg"
    return jsonify({"image_b64": b64, "mime": mime, "prompt_used": final})


def warm_up():
    """Server start par Groq/Sarvam se connection pehle hi khol lo, taaki pehla message slow na ho."""
    try:
        k = os.getenv("GROQ_API_KEY", "").strip()
        if k:
            HTTP.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {k}"}, timeout=10)
        HTTP.get("https://api.sarvam.ai/", timeout=10)
    except Exception:
        pass


init_db()
threading.Thread(target=warm_up, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False, use_reloader=False, threaded=True)
