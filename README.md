# Abhi AI — Hindi Voice Assistant (Flask)

Groq (ya optional Hugging Face) se jawab, Sarvam se Hindi STT/TTS, chat + call mode.
Gali milne par `chala_ja_bhosadike.mp3` chalti hai.

## Files (sab ek hi folder me)
main.py, index.html, call.html, .env, .gitignore, requirements.txt, 4 x .mp3 audio

## Run
```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```
`.env` me keys bharein (GROQ_API_KEY, SARVAM_API_KEY, optional HF_TOKEN), phir http://127.0.0.1:5000 kholein.
Call mode ke liye Chrome use karein.

## Tesseract OCR (+ button)
`+` dabao -> image / PDF / txt / csv chuno -> Abhi file padhkar samjhata hai, phir uske baare me sawal pooch sakte ho.
1. `pip install -r requirements.txt`
2. Tesseract program install karo. Windows: https://github.com/UB-Mannheim/tesseract/wiki (installer me **Hindi** language data zaroor chuno).
3. Na mile to `.env` me `TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe` likho.
