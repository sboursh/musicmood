from flask import Flask, request, jsonify, render_template, redirect, session
from flask_cors import CORS
import requests
import base64
import os
from dotenv import load_dotenv
import secrets

load_dotenv()

app = Flask(__name__)
CORS(app)

CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY")

app.secret_key = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(16))
REDIRECT_URI = "http://127.0.0.1:5000/callback"
SCOPE = "user-library-modify"

# ─── Spotify auth ────────────────────────────────────────────────────────────

def get_spotify_token():
    auth_string = f"{CLIENT_ID}:{CLIENT_SECRET}"
    auth_base64 = base64.b64encode(auth_string.encode()).decode()

    res = requests.post(
        "https://accounts.spotify.com/api/token",
        headers={
            "Authorization": f"Basic {auth_base64}",
            "Content-Type": "application/x-www-form-urlencoded"
        },
        data={"grant_type": "client_credentials"}
    )

    data = res.json()
    if "access_token" not in data:
        raise Exception(f"Spotify token error: {data}")
    return data["access_token"]


# ─── Mood → Last.fm tags ─────────────────────────────────────────────────────

def get_lastfm_tags(valence, energy, genre):
    """
    Map valence + energy coordinates to Last.fm tags.
    Last.fm tags are user-generated and very mood-specific —
    much better than keyword search for actual mood matching.
    """

    # Valence axis (sad → happy)
    if valence >= 0.65:
        valence_tag = "happy"
    elif valence >= 0.45:
        valence_tag = "feel-good"
    elif valence >= 0.25:
        valence_tag = "melancholy"
    else:
        valence_tag = "sad"

    # Energy axis (low → high)
    if energy >= 0.65:
        energy_tag = "energetic"
    elif energy >= 0.45:
        energy_tag = "chill"
    elif energy >= 0.25:
        energy_tag = "relaxing"
    else:
        energy_tag = "sleep"

    # Combined mood tags that exist well on Last.fm
    mood_combos = {
        ("happy",     "energetic"): ["happy",        "party",       "upbeat"],
        ("happy",     "chill"):     ["feel-good",    "sunny",       "positive"],
        ("happy",     "relaxing"):  ["peaceful",     "calm",        "happy"],
        ("happy",     "sleep"):     ["lullaby",      "soft",        "gentle"],
        ("feel-good", "energetic"): ["feel-good",    "motivation",  "pump-up"],
        ("feel-good", "chill"):     ["chill",        "vibes",       "feel-good"],
        ("feel-good", "relaxing"):  ["acoustic",     "mellow",      "easy-listening"],
        ("feel-good", "sleep"):     ["ambient",      "soft",        "dreamy"],
        ("melancholy","energetic"): ["dark",         "intense",     "aggressive"],
        ("melancholy","chill"):     ["melancholy",   "indie",       "moody"],
        ("melancholy","relaxing"):  ["melancholic",  "slow",        "emotional"],
        ("melancholy","sleep"):     ["sad",          "slow",        "lonely"],
        ("sad",       "energetic"): ["angry",        "rage",        "dark"],
        ("sad",       "chill"):     ["sad",          "heartbreak",  "emotional"],
        ("sad",       "relaxing"):  ["sad",          "depressing",  "slow"],
        ("sad",       "sleep"):     ["depressing",   "sad",         "cry"],
    }

    tags = mood_combos.get((valence_tag, energy_tag), [valence_tag, energy_tag])

    # Add genre as an extra tag if provided
    if genre and genre != "any":
        tags = [genre] + tags

    return tags


# ─── Last.fm: get top tracks for a tag ───────────────────────────────────────

def get_lastfm_tracks(tag, limit=50):
    res = requests.get("https://ws.audioscrobbler.com/2.0/", params={
        "method": "tag.getTopTracks",
        "tag": tag,
        "api_key": LASTFM_API_KEY,
        "format": "json",
        "limit": limit,
    })

    data = res.json()
    tracks = data.get("tracks", {}).get("track", [])
    return [(t["name"], t["artist"]["name"]) for t in tracks if t.get("name") and t.get("artist")]


# ─── Spotify: search for a track and get its details ─────────────────────────

def spotify_search(track_name, artist_name, token):
    query = f"track:{track_name} artist:{artist_name}"
    res = requests.get(
        "https://api.spotify.com/v1/search",
        headers={"Authorization": f"Bearer {token}"},
        params={"q": query, "type": "track", "limit": 1, "market": "US"}
    )
    items = res.json().get("tracks", {}).get("items", [])
    if not items:
        return None
    t = items[0]
    return {
        "id": t["id"],   # ← add this
        "name": t["name"],
        "artist": t["artists"][0]["name"],
        "url": t["external_urls"]["spotify"],
        "preview_url": t.get("preview_url"),
        "album_art": t["album"]["images"][0]["url"] if t["album"]["images"] else None,
    }


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/debug")
def debug():
    import requests
    tag = "happy"
    res = requests.get("https://ws.audioscrobbler.com/2.0/", params={
        "method": "tag.getTopTracks",
        "tag": tag,
        "api_key": LASTFM_API_KEY,
        "format": "json",
        "limit": 5,
    })
    return jsonify({
        "api_key_loaded": LASTFM_API_KEY is not None,
        "api_key_preview": LASTFM_API_KEY[:6] + "..." if LASTFM_API_KEY else None,
        "lastfm_status": res.status_code,
        "lastfm_response": res.json()
    })

@app.route("/recommend")
def recommend():
    import random

    try:
        valence = float(request.args.get("valence", 0.5))
        energy = float(request.args.get("energy", 0.5))
        genre = request.args.get("genre", "any")

        # Step 1: get mood-matched tags from Last.fm
        tags = get_lastfm_tags(valence, energy, genre)

        # Step 2: pull top tracks for the primary tag, fallback to others
        lastfm_tracks = []
        for tag in tags:
            lastfm_tracks = get_lastfm_tracks(tag, limit=50)
            if len(lastfm_tracks) >= 10:
                break

        if not lastfm_tracks:
            return jsonify({"error": "No tracks found from Last.fm"}), 404

        # Step 3: shuffle for variety and pick candidates
        random.shuffle(lastfm_tracks)
        candidates = lastfm_tracks[:20]  # look up 20, return best 10

        # Step 4: look each up on Spotify for art/links/preview
        token = get_spotify_token()
        results = []

        for track_name, artist_name in candidates:
            track = spotify_search(track_name, artist_name, token)
            if track:
                results.append(track)
            if len(results) >= 10:
                break

        return jsonify(results)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/login")
def login():
    state = secrets.token_hex(8)
    session["oauth_state"] = state
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "scope": SCOPE,
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "show_dialog": "false",   # ← don't re-ask if already authorized
    }
    from urllib.parse import urlencode
    return redirect("https://accounts.spotify.com/authorize?" + urlencode(params))


@app.route("/callback")
def callback():
    code = request.args.get("code")
    auth_string = f"{CLIENT_ID}:{CLIENT_SECRET}"
    auth_base64 = base64.b64encode(auth_string.encode()).decode()

    res = requests.post("https://accounts.spotify.com/api/token", headers={
        "Authorization": f"Basic {auth_base64}",
        "Content-Type": "application/x-www-form-urlencoded"
    }, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    })

    data = res.json()
    session["user_token"] = data.get("access_token")
    return redirect("/")


@app.route("/like", methods=["POST"])
def like():
    token = session.get("user_token")
    if not token:
        return jsonify({"error": "not_logged_in"}), 401

    track_id = request.json.get("track_id")
    if not track_id:
        return jsonify({"error": "no track_id"}), 400

    res = requests.put(
        "https://api.spotify.com/v1/me/tracks",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"ids": [track_id]}
    )

    print("LIKE STATUS:", res.status_code)
    print("LIKE BODY:", res.text)

    # Spotify returns 200 on success, body is empty
    return jsonify({"ok": res.status_code == 200})

if __name__ == "__main__":
    app.run(debug=True)