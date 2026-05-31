import os
import tempfile
from flask import (
    Flask,
    request,
    jsonify,
    render_template,
    session,
    redirect,
    url_for,
    Response,
    stream_with_context,
    send_file,
)
from flask_cors import CORS
import mysql.connector
import json
import requests
from functools import wraps

IS_VERCEL = os.getenv("VERCEL") == "1"
HF_BACKEND_URL = os.getenv("HF_BACKEND_URL", "").strip().rstrip("/")

if not IS_VERCEL:
    from ollama import chat
    from scripts.pipeline.pipeline import run_pipeline_stream
    from scripts.pipeline.audio_input import Transcriber
    from scripts.utils.ocr_extractor import extract_text_with_ocr
    from scripts.utils.tts_generator import generate_audio

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "shikhbo-local-dev-secret")
CORS(app)

# ─── DB Config ──
DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "",
    "database": "sikhbo",
}


def get_db():
    return mysql.connector.connect(**DB_CONFIG)


def backend_url(path):
    if not HF_BACKEND_URL:
        return None
    if HF_BACKEND_URL.endswith(path):
        return HF_BACKEND_URL
    return f"{HF_BACKEND_URL}{path}"


def demo_disabled_response(feature):
    return (
        jsonify(
            {
                "error": f"{feature} is disabled in the Vercel frontend demo and will be available through the Hugging Face backend."
            }
        ),
        503,
    )


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)

    return decorated


# ─── Page Routes
@app.route("/")
def index():
    if "user" in session:
        return redirect(url_for("chat_route"))
    return render_template("index.html")


@app.route("/chat")
def chat_route():
    if "user" not in session:
        return redirect(url_for("index"))
    return render_template("chat.html")


@app.route("/favicon.ico")
@app.route("/favicon.png")
def favicon():
    return ("", 204)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "shikhbo-vercel"})


@app.route("/api/me", methods=["GET"])
@login_required
def get_me():
    return jsonify({"user": session.get("user")})


# ─── Auth API
@app.route("/api/signup", methods=["POST"])
def signup():
    data = request.get_json()
    name = data.get("name", "").strip()
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()
    class_ = data.get("class", "").strip()
    curriculum = data.get("curriculum", "").strip()

    if not all([name, email, password, class_, curriculum]):
        return jsonify({"error": "All fields are required."}), 400

    if IS_VERCEL:
        session["user"] = {
            "uid": 1,
            "name": name,
            "email": email,
            "class": class_,
            "curriculum": curriculum,
        }
        return jsonify({"message": "Account created.", "user": session["user"]}), 201

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO students (name, email, password, class, curriculum) VALUES (%s, %s, %s, %s, %s)",
            (name, email, password, class_, curriculum),
        )
        conn.commit()
        uid = cur.lastrowid
        cur.close()
        conn.close()

        session["user"] = {
            "uid": uid,
            "name": name,
            "email": email,
            "class": class_,
            "curriculum": curriculum,
        }
        return jsonify({"message": "Account created.", "user": session["user"]}), 201

    except mysql.connector.IntegrityError:
        return jsonify({"error": "Email already registered."}), 409
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json()
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()

    if not email or not password:
        return jsonify({"error": "Email and password required."}), 400

    # Demo Mode: Rule Based Mechanism
    if email == "test@shikhbo.com" and password == "Test@Shikhobo":
        session["user"] = {
            "uid": 1,
            "name": "Kausar Ahmed",
            "email": "test@shikhbo.com",
            "class": "SSC",
            "curriculum": "National",
        }
        return jsonify({"message": "Login successful.", "user": session["user"]}), 200
    else:
        return jsonify({"error": "Invalid email or password (use test account)."}), 401

    """
    try:
        conn = get_db()
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT * FROM students WHERE email=%s AND password=%s", (email, password)
        )
        user = cur.fetchone()
        cur.close()
        conn.close()

        if not user:
            return jsonify({"error": "Invalid email or password."}), 401

        session["user"] = {
            "uid": user["uid"],
            "name": user["name"],
            "email": user["email"],
            "class": user["class"],
            "curriculum": user["curriculum"],
        }
        return jsonify({"message": "Login successful.", "user": session["user"]}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    """


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"message": "Logged out."}), 200


@app.route("/api/update_profile", methods=["POST"])
@login_required
def update_profile():
    data = request.get_json()
    user = session["user"]

    name = data.get("name", "").strip() or user["name"]
    class_ = data.get("class", "").strip() or user["class"]
    curriculum = data.get("curriculum", "").strip() or user["curriculum"]

    if IS_VERCEL:
        session["user"]["name"] = name
        session["user"]["class"] = class_
        session["user"]["curriculum"] = curriculum
        session.modified = True
        return (
            jsonify(
                {"message": "Profile updated successfully.", "user": session["user"]}
            ),
            200,
        )

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "UPDATE students SET name=%s, class=%s, curriculum=%s WHERE uid=%s",
            (name, class_, curriculum, user["uid"]),
        )
        conn.commit()
        cur.close()
        conn.close()

        session["user"]["name"] = name
        session["user"]["class"] = class_
        session["user"]["curriculum"] = curriculum
        session.modified = True

        return (
            jsonify(
                {"message": "Profile updated successfully.", "user": session["user"]}
            ),
            200,
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Chat API
@app.route("/api/query", methods=["POST"])
@login_required
def query():
    data = request.get_json()
    user = session.get("user")

    messages = data.get("messages", [])

    if not messages:
        user_query = data.get("query", "").strip()
        if not user_query:
            return jsonify({"error": "Query or messages are required."}), 400
        messages = [{"role": "user", "content": user_query}]

    latest_query = messages[-1]["content"] if messages else ""

    user_json = {
        "uid": user["uid"],
        "name": user["name"],
        "class_level": user["class"],
        "curriculum": user["curriculum"],
        "subject": data.get("subject", ""),
        "mode": data.get("mode", "normal"),
        "response_quality": data.get("response_quality", "enhanced"),
        "query": latest_query,
        "messages": messages,
    }

    if IS_VERCEL:
        target_url = backend_url("/api/query")

        def generate_demo():
            if not target_url:
                yield json.dumps(
                    {
                        "chunk": "The Shikhbo Vercel frontend is live. The full AI backend will be connected through Hugging Face Spaces."
                    }
                ) + "\n"
                return

            try:
                with requests.post(
                    target_url,
                    json=user_json,
                    stream=True,
                    timeout=(5, 30),
                ) as response:
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "")
                    if "application/json" in content_type:
                        payload = response.json()
                        if isinstance(payload, dict) and "chunk" in payload:
                            yield json.dumps(payload) + "\n"
                        elif isinstance(payload, dict):
                            yield json.dumps({"chunk": payload.get("answer", str(payload))}) + "\n"
                        else:
                            yield json.dumps({"chunk": str(payload)}) + "\n"
                        return

                    for line in response.iter_lines():
                        if line:
                            if isinstance(line, bytes):
                                line = line.decode("utf-8", errors="ignore")
                            yield line + "\n"
            except Exception as e:
                yield json.dumps({"chunk": f"\n[Error: {str(e)}]"}) + "\n"

        return Response(
            stream_with_context(generate_demo()), mimetype="application/x-ndjson"
        )

    def generate():
        try:
            for payload in run_pipeline_stream(user_json):
                yield json.dumps(payload) + "\n"
        except Exception as e:
            yield json.dumps({"chunk": f"\n[Error: {str(e)}]"}) + "\n"

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson")


@app.route("/api/transcribe", methods=["POST"])
@login_required
def transcribe_audio():
    if IS_VERCEL and not HF_BACKEND_URL:
        return demo_disabled_response("Speech transcription")

    if "audio" not in request.files:
        return jsonify({"error": "No audio file provided."}), 400

    audio_file = request.files["audio"]
    lang = request.form.get("lang", "en")

    if audio_file.filename == "":
        return jsonify({"error": "Empty audio file."}), 400

    if IS_VERCEL:
        target_url = backend_url("/api/transcribe")
        try:
            response = requests.post(
                target_url,
                files={
                    "audio": (
                        audio_file.filename,
                        audio_file.stream,
                        audio_file.mimetype,
                    )
                },
                data={"lang": lang},
                timeout=(5, 30),
            )
            return Response(
                response.content,
                status=response.status_code,
                content_type=response.headers.get("content-type", "application/json"),
            )
        except Exception as e:
            return jsonify({"error": str(e)}), 502

    try:
        # Save temp file
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, f"temp_audio_{session['user']['uid']}.wav")
        audio_file.save(temp_path)

        # Transcribe
        transcriber = Transcriber()
        text = transcriber.transcribe(temp_path, lang)

        # Cleanup
        if os.path.exists(temp_path):
            os.remove(temp_path)

        return jsonify({"text": text}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/extract_file", methods=["POST"])
@login_required
def extract_file():
    if IS_VERCEL and not HF_BACKEND_URL:
        return demo_disabled_response("File text extraction")

    if "file" not in request.files:
        return jsonify({"error": "No file provided."}), 400

    uploaded_file = request.files["file"]
    if uploaded_file.filename == "":
        return jsonify({"error": "Empty file."}), 400

    if IS_VERCEL:
        target_url = backend_url("/api/extract_file")
        try:
            response = requests.post(
                target_url,
                files={
                    "file": (
                        uploaded_file.filename,
                        uploaded_file.stream,
                        uploaded_file.mimetype,
                    )
                },
                timeout=(5, 30),
            )
            return Response(
                response.content,
                status=response.status_code,
                content_type=response.headers.get("content-type", "application/json"),
            )
        except Exception as e:
            return jsonify({"error": str(e)}), 502

    try:
        text = extract_text_with_ocr(uploaded_file)
        if text is None:
            return jsonify({"error": "Could not extract text from file."}), 500
        return jsonify({"text": text}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tts", methods=["POST"])
@login_required
def extract_tts():
    if IS_VERCEL and not HF_BACKEND_URL:
        return demo_disabled_response("Text to speech")

    data = request.get_json()
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "No text provided."}), 400

    if IS_VERCEL:
        target_url = backend_url("/api/tts")
        try:
            response = requests.post(
                target_url,
                json={"text": text},
                timeout=(5, 30),
            )
            return Response(
                response.content,
                status=response.status_code,
                content_type=response.headers.get("content-type", "audio/wav"),
            )
        except Exception as e:
            return jsonify({"error": str(e)}), 502

    try:
        audio_io = generate_audio(text)
        if not audio_io:
            return (
                jsonify({"error": "TTS engine not initialized or model missing."}),
                500,
            )

        return send_file(
            audio_io,
            mimetype="audio/wav",
            as_attachment=False,
            download_name="speech.wav",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000, host="0.0.0.0")
