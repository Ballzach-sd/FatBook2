from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime
import os

import dotenv
import requests
import json
import sys
import os
from dotenv import load_dotenv
import re
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
import time

from flask_socketio import SocketIO, emit, join_room


# Load environment variables from .env file
load_dotenv(override=True)

import stripe

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")


# --- Config ---
AGENT_ENDPOINT = os.getenv("ENDPOINT_URL")
AGENT_ACCESS_KEY = os.getenv("ACCESS_KEY")  # Replace with your actual access key
USER_MESSAGE = "Hey how are you?"


# --- Step 1: Send message to agent ---
def get_agent_response(message):
    url = f"{AGENT_ENDPOINT}/api/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {AGENT_ACCESS_KEY}",
    }
    payload = {
        "messages": [{"role": "user", "content": message}],
        "stream": False,
        "include_functions_info": False,
        "include_retrieval_info": False,
        "include_guardrails_info": False,
    }

    response = requests.post(url, headers=headers, json=payload)
    response.raise_for_status()
    data = response.json()
    raw = data["choices"][0]["message"]["content"]

    # Strip <think>...</think>
    clean = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    return clean.strip()


# --- Step 2: Extract search query from agent response ---
def extract_query(response_text):
    match = re.search(r"SEARCH_QUERY\s*:\s*(.+)", response_text)
    return match.group(1).strip() if match else None


# --- Step 3: Marketplace search ---
def search_facebook_marketplace(query):
    formatted_query = query.replace(" ", "_")
    url = f"https://www.facebook.com/marketplace/search/?query={formatted_query}"

    options = Options()
    options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1200x800")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)

    try:
        driver.get(url)
        time.sleep(5)

        try:
            close_button = driver.find_element(By.XPATH, '//div[@aria-label="Close"]')
            close_button.click()
            time.sleep(2)
        except:
            pass

        try:
            not_now = driver.find_element(
                By.XPATH,
                '//div[contains(text(), "Not Now") or contains(text(), "Cancel")]',
            )
            not_now.click()
            time.sleep(2)
        except:
            pass

        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(4)

        soup = BeautifulSoup(driver.page_source, "html.parser")
        items = []

        for a in soup.find_all("a", href=True):
            title = a.get_text(strip=True)
            if title and "marketplace" not in title.lower():
                href = a["href"]
                if href.startswith("/marketplace/item"):
                    items.append({"title": title, "url": "https://facebook.com" + href})

        seen = set()
        unique_items = []
        for item in items:
            if item["title"] not in seen:
                seen.add(item["title"])
                unique_items.append(item)
            if len(unique_items) >= 10:
                break

        return unique_items
    finally:
        driver.quit()


app = Flask(__name__)
app.secret_key = "supersecretkey"

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

from functools import wraps
from flask import abort


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("auth"))
        user = User.query.get(session["user_id"])
        if not user or user.username != "Ballzach":
            return abort(403)
        return f(*args, **kwargs)

    return decorated_function


# Upload folders
UPLOAD_FOLDER = os.path.join("fatbuk/static", "uploads")
AVATAR_FOLDER = os.path.join("fatbuk/static", "avatars")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(AVATAR_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["AVATAR_FOLDER"] = AVATAR_FOLDER

# SQLite DB setup
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///fatbuk.db"
db = SQLAlchemy(app)

# ---------------------- MODELS ----------------------


class AccessKey(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(64), unique=True, nullable=False)
    used = db.Column(db.Boolean, default=False)
    creator_id = db.Column(db.Integer, db.ForeignKey("user.id"))


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    profile_picture = db.Column(db.String(200), nullable=True)
    fatbucks = db.Column(db.Integer, default=0)
    muted_until = db.Column(db.DateTime, nullable=True)


class Friend(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    friend_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    accepted = db.Column(db.Boolean, default=False)


class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    content = db.Column(db.Text, nullable=False)
    image_url = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship("User", backref="posts")
    medals = db.Column(db.Integer, default=0)


class Like(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    post_id = db.Column(db.Integer, db.ForeignKey("post.id"))
    is_like = db.Column(db.Boolean, nullable=False)


class Comment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    post_id = db.Column(db.Integer, db.ForeignKey("post.id"))
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship("User")


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    receiver_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    sender = db.relationship("User", foreign_keys=[sender_id])
    receiver = db.relationship("User", foreign_keys=[receiver_id])


class Group(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.Text, nullable=True)


class GroupInvite(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("group.id"))
    invited_user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    inviter_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class GroupMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("group.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))


class GroupEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("group.id"))
    title = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    date = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class GroupMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("group.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User")


import secrets


class CallRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    caller_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    receiver_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    room_id = db.Column(
        db.String(32), unique=True, nullable=False, default=lambda: secrets.token_hex(8)
    )
    active = db.Column(db.Boolean, default=True)
    accepted = db.Column(db.Boolean, default=None)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)


# ---------------------- ROUTES ----------------------


@app.route("/")
def index():
    if "user_id" not in session:
        return redirect(url_for("auth"))

    posts = Post.query.order_by(Post.created_at.desc()).all()
    for post in posts:
        post.likes = Like.query.filter_by(post_id=post.id, is_like=True).count()
        post.dislikes = Like.query.filter_by(post_id=post.id, is_like=False).count()
        post.comments = Comment.query.filter_by(post_id=post.id).all()
    user = User.query.get(session["user_id"])
    return render_template("index.html", user=user, posts=posts)


@app.route("/marketplace", methods=["GET", "POST"])
def ai_marketplace():
    if "user_id" not in session:
        return redirect(url_for("auth"))

    if "chat" not in session:
        session["chat"] = []

    listings = []

    if request.method == "POST":
        user_msg = request.form["message"]
        session["chat"].append({"role": "user", "content": user_msg})

        # Step 1: Call LLM and clean response
        ai_response = get_agent_response(user_msg)
        ai_response = re.sub(
            r"<think>.*?</think>", "", ai_response, flags=re.DOTALL
        ).strip()
        session["chat"].append({"role": "ai", "content": ai_response})

        # Step 2: Extract query and reset listings
        query = extract_query(ai_response)
        listings = []  # Clear old listings

        # Step 3: Search with new query
        if query:
            listings = search_facebook_marketplace(query)

    return render_template(
        "ai_marketplace.html", chat=session["chat"], listings=listings
    )


import secrets


@app.route("/buy_invite", methods=["POST"])
def buy_invite():
    if "user_id" not in session:
        return redirect(url_for("auth"))

    new_key = secrets.token_hex(16)
    db.session.add(AccessKey(key=new_key, creator_id=session["user_id"]))
    db.session.commit()

    return redirect(url_for("store"))  # ✅ NEW


@app.route("/store")
def store():
    if "user_id" not in session:
        return redirect(url_for("auth"))

    user = User.query.get(session["user_id"])
    keys = (
        AccessKey.query.filter_by(creator_id=user.id)
        .order_by(AccessKey.used.asc(), AccessKey.id.desc())
        .all()
    )
    new_key = session.pop("new_invite_key", None)  # show once
    return render_template("store.html", user=user, keys=keys, new_key=new_key)


@app.route("/buy_fatbux/<int:bundle_id>", methods=["POST"])
def buy_fatbux(bundle_id):
    if "user_id" not in session:
        return redirect(url_for("auth"))

    bundles = {
        1: {"amount": 199, "quantity": 1000, "label": "1000 Fatbux"},
        2: {"amount": 799, "quantity": 5000, "label": "5000 Fatbux"},
        3: {"amount": 999, "quantity": 10000, "label": "10000 Fatbux"},
    }

    if bundle_id not in bundles:
        return "Invalid bundle", 400

    bundle = bundles[bundle_id]

    checkout_session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="payment",
        line_items=[
            {
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": bundle["label"]},
                    "unit_amount": bundle["amount"],
                },
                "quantity": 1,
            }
        ],
        success_url=url_for("store_success", _external=True),
        cancel_url=url_for("store", _external=True),
        metadata={"user_id": session["user_id"], "fatbux_amount": bundle["quantity"]},
    )

    # Save the current bundle ID and amount in Flask session
    session["pending_fatbux"] = bundle["quantity"]
    return redirect(checkout_session.url, code=303)


@app.route("/store_success")
def store_success():
    if "user_id" not in session:
        return redirect(url_for("auth"))

    qty = session.pop("pending_fatbux", None)

    if not qty:
        return "No purchase found in session.", 400

    user = User.query.get(session["user_id"])
    user.fatbucks += int(qty)
    db.session.commit()

    return render_template("store_success.html", user=user)


@app.route("/admin", methods=["GET", "POST"])
@admin_required
def admin_panel():
    if request.method == "POST":
        new_key = secrets.token_hex(16)
        db.session.add(AccessKey(key=new_key))
        db.session.commit()
    keys = AccessKey.query.all()
    users = User.query.all()
    posts = Post.query.order_by(Post.created_at.desc()).all()
    return render_template("admin_panel.html", users=users, posts=posts, keys=keys)


@app.context_processor
def inject_user():
    if "user_id" in session:
        return {"user": User.query.get(session["user_id"])}
    return {"user": None}


from datetime import datetime


@app.context_processor
def inject_now():
    return {"now": datetime.utcnow}


@app.route("/voice")
def voice_chat():
    if "user_id" not in session:
        return redirect(url_for("auth"))

    user_id = session["user_id"]

    # Get confirmed friends
    relationships = Friend.query.filter(
        ((Friend.user_id == user_id) | (Friend.friend_id == user_id))
        & (Friend.accepted == True)
    ).all()
    friend_ids = [
        f.friend_id if f.user_id == user_id else f.user_id for f in relationships
    ]
    friends = User.query.filter(User.id.in_(friend_ids)).all()

    return render_template("voice_chat.html", friends=friends)


@socketio.on("join")
def handle_join(data):
    room = data["room"]
    print("👥 Joined room:", room)
    join_room(room)
    emit("new-user", {}, to=room, skip_sid=request.sid)


@socketio.on("signal")
def handle_signal(data):
    emit("signal", data, to=data["room"], skip_sid=request.sid)


@app.route("/call/<int:friend_id>", methods=["POST"])
def start_call(friend_id):
    if "user_id" not in session:
        return "", 403

    # Remove old calls
    CallRequest.query.filter_by(caller_id=session["user_id"]).delete()
    db.session.commit()

    room_id = secrets.token_hex(8)
    call = CallRequest(
        caller_id=session["user_id"], receiver_id=friend_id, room_id=room_id
    )
    db.session.add(call)
    db.session.commit()
    return jsonify({"room_id": room_id})


@app.route("/check_call")
def check_call():
    if "user_id" not in session:
        return "", 403

    call = CallRequest.query.filter_by(
        receiver_id=session["user_id"], active=True
    ).first()
    if call:
        return jsonify(
            {
                "caller": User.query.get(call.caller_id).username,
                "caller_id": call.caller_id,
                "accepted": call.accepted,
                "room_id": call.room_id,
            }
        )
    return jsonify({})


@app.route("/call_status/<int:receiver_id>")
def call_status(receiver_id):
    if "user_id" not in session:
        return "", 403

    call = CallRequest.query.filter_by(
        caller_id=session["user_id"], receiver_id=receiver_id, active=True
    ).first()
    if call:
        return jsonify(
            {"accepted": call.accepted, "active": call.active, "room_id": call.room_id}
        )
    return jsonify({"accepted": None, "active": False})


from flask import jsonify


@app.route("/call_room/<room_id>")
def call_room(room_id):
    if "user_id" not in session:
        return redirect(url_for("auth"))

    call = CallRequest.query.filter_by(room_id=room_id, active=True).first()
    if not call:
        return "Call not found or expired.", 404

    return render_template("call_room.html", room_id=room_id)


@app.route("/accept_call/<int:caller_id>", methods=["POST"])
def accept_call(caller_id):
    if "user_id" not in session:
        return "", 403

    call = CallRequest.query.filter_by(
        caller_id=caller_id, receiver_id=session["user_id"], active=True
    ).first()
    if call:
        call.accepted = True
        db.session.commit()
        return jsonify({"redirect": url_for("call_room", room_id=call.room_id)})
    return jsonify({"error": "Call not found"}), 404


@app.route("/decline_call/<int:caller_id>", methods=["POST"])
def decline_call(caller_id):
    if "user_id" not in session:
        return "", 403

    call = CallRequest.query.filter_by(
        caller_id=caller_id, receiver_id=session["user_id"], active=True
    ).first()
    if call:
        call.accepted = False
        call.active = False
        db.session.commit()
        return "Declined", 200
    return "Call not found", 404


@app.route("/call_page/<int:friend_id>")
def call_page(friend_id):
    if "user_id" not in session:
        return redirect(url_for("auth"))

    friend = User.query.get_or_404(friend_id)
    return render_template("call_page.html", friend=friend)


@app.route("/groups")
def groups():
    if "user_id" not in session:
        return redirect(url_for("auth"))

    user_id = session["user_id"]

    memberships = GroupMember.query.filter_by(user_id=user_id).all()
    joined_groups = [Group.query.get(m.group_id) for m in memberships]

    # Show groups they've been invited to but not joined
    invites = GroupInvite.query.filter_by(invited_user_id=user_id).all()
    invited_groups = [Group.query.get(i.group_id) for i in invites]

    return render_template(
        "groups.html", groups=joined_groups, invited_groups=invited_groups
    )


@app.route("/groups/new", methods=["GET", "POST"])
def create_group():
    if "user_id" not in session:
        return redirect(url_for("auth"))

    if request.method == "POST":
        name = request.form["name"]
        description = request.form["description"]

        if Group.query.filter_by(name=name).first():
            return "Group name already exists."

        group = Group(name=name, description=description)
        db.session.add(group)
        db.session.commit()

        # Auto-add creator as member
        db.session.add(GroupMember(user_id=session["user_id"], group_id=group.id))
        db.session.commit()

        return redirect(url_for("group_detail", group_id=group.id))

    return render_template("create_group.html")


@app.route("/groups/<int:group_id>", methods=["GET", "POST"])
def group_detail(group_id):
    if "user_id" not in session:
        return redirect(url_for("auth"))

    group = Group.query.get_or_404(group_id)
    is_member = GroupMember.query.filter_by(
        group_id=group_id, user_id=session["user_id"]
    ).first()
    if not is_member:
        return "Access denied", 403

    members = GroupMember.query.filter_by(group_id=group_id).all()
    users = [User.query.get(m.user_id) for m in members]

    events = (
        GroupEvent.query.filter_by(group_id=group_id)
        .order_by(GroupEvent.date.asc())
        .all()
    )
    messages = (
        GroupMessage.query.filter_by(group_id=group_id)
        .order_by(GroupMessage.timestamp.asc())
        .all()
    )

    return render_template(
        "group_detail.html",
        group=group,
        members=users,
        events=events,
        messages=messages,
    )


from datetime import datetime


@app.route("/groups/<int:group_id>/event", methods=["POST"])
def add_group_event(group_id):
    if "user_id" not in session:
        return redirect(url_for("auth"))

    title = request.form["title"]
    description = request.form["description"]
    date_str = request.form["date"]

    try:
        # Convert YYYY-MM-DD string to a datetime object
        event_date = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return "Invalid date format", 400

    db.session.add(
        GroupEvent(
            group_id=group_id, title=title, description=description, date=event_date
        )
    )
    db.session.commit()
    return redirect(url_for("group_detail", group_id=group_id))


@app.route("/groups/<int:group_id>/chat", methods=["POST"])
def send_group_message(group_id):
    if "user_id" not in session:
        return redirect(url_for("auth"))

    content = request.form["message"]
    db.session.add(
        GroupMessage(group_id=group_id, user_id=session["user_id"], content=content)
    )
    db.session.commit()
    return redirect(url_for("group_detail", group_id=group_id))


from flask import render_template_string


@app.route("/groups/<int:group_id>/chat_feed")
def chat_feed(group_id):
    if "user_id" not in session:
        return "", 403
    messages = (
        GroupMessage.query.filter_by(group_id=group_id)
        .order_by(GroupMessage.timestamp.asc())
        .all()
    )
    return render_template("components/group_chat.html", messages=messages)


@app.route("/groups/<int:group_id>/invite", methods=["POST"])
def invite_to_group(group_id):
    if "user_id" not in session:
        return redirect(url_for("auth"))

    username = request.form["username"]
    invited_user = User.query.filter_by(username=username).first()

    if not invited_user:
        return "User not found", 404

    # Check if already a member
    if GroupMember.query.filter_by(group_id=group_id, user_id=invited_user.id).first():
        return "User already in group", 400

    # Check if already invited
    if GroupInvite.query.filter_by(
        group_id=group_id, invited_user_id=invited_user.id
    ).first():
        return "User already invited", 400

    invite = GroupInvite(
        group_id=group_id,
        invited_user_id=invited_user.id,
        inviter_id=session["user_id"],
    )
    db.session.add(invite)
    db.session.commit()
    return redirect(url_for("group_detail", group_id=group_id))


@app.route("/groups/accept_invite/<int:group_id>")
def accept_group_invite(group_id):
    if "user_id" not in session:
        return redirect(url_for("auth"))

    user_id = session["user_id"]

    # Check for a valid invite
    invite = GroupInvite.query.filter_by(
        group_id=group_id, invited_user_id=user_id
    ).first()
    if not invite:
        return "No invitation found", 404

    # Add as member
    db.session.add(GroupMember(user_id=user_id, group_id=group_id))
    db.session.delete(invite)
    db.session.commit()

    return redirect(url_for("group_detail", group_id=group_id))


@app.route("/groups/join/<int:group_id>")
def join_group(group_id):
    if "user_id" not in session:
        return redirect(url_for("auth"))

    existing = GroupMember.query.filter_by(
        user_id=session["user_id"], group_id=group_id
    ).first()
    if not existing:
        db.session.add(GroupMember(user_id=session["user_id"], group_id=group_id))
        db.session.commit()
    return redirect(url_for("groups"))


@app.route("/groups/leave/<int:group_id>")
def leave_group(group_id):
    if "user_id" not in session:
        return redirect(url_for("auth"))

    GroupMember.query.filter_by(user_id=session["user_id"], group_id=group_id).delete()
    db.session.commit()
    return redirect(url_for("groups"))


@app.route("/create_post", methods=["POST"])
def create_post():
    if "user_id" not in session:
        return redirect(url_for("auth"))

    user = User.query.get(session["user_id"])
    now = datetime.utcnow()

    if user.muted_until and user.muted_until > now:
        return "You are muted and cannot post right now.", 403

    content = request.form["content"]
    image = request.files.get("image")
    image_filename = None

    if image and image.filename:
        image_filename = secure_filename(image.filename)
        image.save(os.path.join(app.config["UPLOAD_FOLDER"], image_filename))

    new_post = Post(user_id=user.id, content=content.strip(), image_url=image_filename)
    db.session.add(new_post)
    db.session.commit()
    return redirect(url_for("index"))


from datetime import datetime, timedelta


@app.route("/post_action/<int:post_id>/<string:action>")
def post_action(post_id, action):
    if "user_id" not in session:
        return redirect(url_for("auth"))

    user = User.query.get(session["user_id"])
    post = Post.query.get_or_404(post_id)
    target_user = post.user

    now = datetime.utcnow()

    if action == "medal":
        if user.fatbucks < 50:
            return "Not enough Fatbux for medal", 400
        post.medals += 1
        user.fatbucks -= 50
        db.session.commit()
        return redirect(url_for("index"))

    elif action == "mute":
        # Check cooldown
        if target_user.muted_until and target_user.muted_until > now:
            return f"{target_user.username} is already muted.", 400

        if user.fatbucks < 100:
            return "Not enough Fatbux to mute", 400

        target_user.muted_until = now + timedelta(minutes=5)
        user.fatbucks -= 100
        db.session.commit()
        return redirect(url_for("index"))

    return "Invalid action", 400


@app.route("/like/<int:post_id>/<action>")
def like(post_id, action):
    if "user_id" not in session:
        return redirect(url_for("auth"))

    is_like = action == "like"
    existing = Like.query.filter_by(user_id=session["user_id"], post_id=post_id).first()
    if existing:
        db.session.delete(existing)
    db.session.add(Like(user_id=session["user_id"], post_id=post_id, is_like=is_like))
    db.session.commit()
    return redirect(url_for("index"))


@app.route("/comment/<int:post_id>", methods=["POST"])
def comment(post_id):
    if "user_id" not in session:
        return redirect(url_for("auth"))
    content = request.form["comment"]
    db.session.add(
        Comment(user_id=session["user_id"], post_id=post_id, content=content.strip())
    )
    db.session.commit()
    return redirect(url_for("index"))


@app.route("/profile", methods=["GET", "POST"])
def profile():
    if "user_id" not in session:
        return redirect(url_for("auth"))

    user = User.query.get(session["user_id"])

    if request.method == "POST":
        username = request.form["username"]
        email = request.form["email"]
        avatar = request.files.get("avatar")

        if avatar and avatar.filename:
            avatar_filename = secure_filename(avatar.filename)
            avatar_path = os.path.join(app.config["AVATAR_FOLDER"], avatar_filename)
            avatar.save(avatar_path)
            user.profile_picture = avatar_filename

        if (
            username != user.username
            and not User.query.filter_by(username=username).first()
        ):
            user.username = username
        if email != user.email and not User.query.filter_by(email=email).first():
            user.email = email

        db.session.commit()
        return redirect(url_for("profile"))

    return render_template("profile.html", user=user)


@app.route("/user/<int:user_id>")
def user_profile(user_id):
    if "user_id" not in session:
        return redirect(url_for("auth"))

    current_user_id = session["user_id"]
    profile_user = User.query.get_or_404(user_id)
    posts = Post.query.filter_by(user_id=user_id).order_by(Post.created_at.desc()).all()

    # Get all friend relationships for the current user
    relationships = Friend.query.filter(
        (Friend.user_id == current_user_id) | (Friend.friend_id == current_user_id)
    ).all()

    sent = [
        f.friend_id
        for f in relationships
        if f.user_id == current_user_id and not f.accepted
    ]
    received = [
        f.user_id
        for f in relationships
        if f.friend_id == current_user_id and not f.accepted
    ]
    confirmed = [
        f.friend_id if f.user_id == current_user_id else f.user_id
        for f in relationships
        if f.accepted
    ]

    return render_template(
        "user_profile.html",
        profile_user=profile_user,
        posts=posts,
        sent=sent,
        received=received,
        confirmed=confirmed,
    )


@app.route("/friends")
def friends():
    if "user_id" not in session:
        return redirect(url_for("auth"))
    user = User.query.get(session["user_id"])
    all_users = User.query.filter(User.id != user.id).all()

    relationships = Friend.query.filter(
        (Friend.user_id == user.id) | (Friend.friend_id == user.id)
    ).all()

    sent = [
        f.friend_id for f in relationships if f.user_id == user.id and not f.accepted
    ]
    received = [
        f.user_id for f in relationships if f.friend_id == user.id and not f.accepted
    ]
    confirmed = [
        f.friend_id if f.user_id == user.id else f.user_id
        for f in relationships
        if f.accepted
    ]

    return render_template(
        "friends.html",
        user=user,
        all_users=all_users,
        sent=sent,
        received=received,
        confirmed=confirmed,
    )


@app.route("/add_friend/<int:friend_id>")
def add_friend(friend_id):
    if "user_id" not in session:
        return redirect(url_for("auth"))

    existing = Friend.query.filter_by(
        user_id=session["user_id"], friend_id=friend_id
    ).first()
    if not existing:
        db.session.add(Friend(user_id=session["user_id"], friend_id=friend_id))
        db.session.commit()
    return redirect(url_for("friends"))


@app.route("/accept_friend/<int:friend_id>")
def accept_friend(friend_id):
    if "user_id" not in session:
        return redirect(url_for("auth"))

    request_obj = Friend.query.filter_by(
        user_id=friend_id, friend_id=session["user_id"], accepted=False
    ).first()
    if request_obj:
        request_obj.accepted = True
        db.session.commit()
    return redirect(url_for("friends"))


@app.route("/messages")
def messages():
    if "user_id" not in session:
        return redirect(url_for("auth"))
    user_id = session["user_id"]

    # Only show confirmed friends
    relationships = Friend.query.filter(
        ((Friend.user_id == user_id) | (Friend.friend_id == user_id))
        & (Friend.accepted == True)
    ).all()
    friend_ids = [
        f.friend_id if f.user_id == user_id else f.user_id for f in relationships
    ]
    friends = User.query.filter(User.id.in_(friend_ids)).all()

    return render_template("messages.html", friends=friends)


@app.route("/messages/<int:friend_id>", methods=["GET", "POST"])
def chat(friend_id):
    if "user_id" not in session:
        return redirect(url_for("auth"))
    user_id = session["user_id"]

    friend = User.query.get_or_404(friend_id)

    # Send message
    if request.method == "POST":
        content = request.form["message"]
        db.session.add(
            Message(sender_id=user_id, receiver_id=friend_id, content=content)
        )
        db.session.commit()
        return redirect(url_for("chat", friend_id=friend_id))

    # Get conversation
    messages = (
        Message.query.filter(
            ((Message.sender_id == user_id) & (Message.receiver_id == friend_id))
            | ((Message.sender_id == friend_id) & (Message.receiver_id == user_id))
        )
        .order_by(Message.timestamp.asc())
        .all()
    )

    return render_template("chat.html", friend=friend, messages=messages)


@app.route("/auth")
def auth():
    return render_template("auth.html")


@app.route("/login", methods=["POST"])
def login():
    username = request.form["username"]
    password = request.form["password"]
    user = User.query.filter_by(username=username).first()
    if user and check_password_hash(user.password, password):
        session["user_id"] = user.id
        return redirect(url_for("index"))
    return 'Invalid credentials. <a href="/auth">Try again</a>'


@app.route("/register", methods=["POST"])
def register():
    username = request.form["username"]
    email = request.form["email"]
    password = generate_password_hash(request.form["password"])
    avatar = request.files.get("avatar")
    access_key = request.form.get("access_key", "").strip()

    existing_users = User.query.count()

    # 🔐 If this is NOT the first user, enforce access key
    if existing_users > 0:
        key = AccessKey.query.filter_by(key=access_key, used=False).first()
        if not key:
            return 'Invalid or used access key. <a href="/auth">Try again</a>'
        key.used = True  # Mark the key as used

    # 🔁 Check for existing user or email
    if (
        User.query.filter_by(username=username).first()
        or User.query.filter_by(email=email).first()
    ):
        return 'User already exists. <a href="/auth">Go back</a>'

    avatar_filename = None
    if avatar and avatar.filename:
        avatar_filename = secure_filename(avatar.filename)
        avatar.save(os.path.join(app.config["AVATAR_FOLDER"], avatar_filename))

    new_user = User(
        username=username,
        email=email,
        password=password,
        profile_picture=avatar_filename,
    )

    db.session.add(new_user)
    db.session.commit()
    return redirect(url_for("auth"))


@app.route("/logout")
def logout():
    session.pop("user_id", None)
    return redirect(url_for("auth"))


@app.route("/terms/")
def terms_of_service():
    return render_template("tos.html")


@app.route("/privacy/")
def privacy_policy():
    return render_template("privacy.html")


@app.route("/about/")
def about():
    return render_template("about.html")


# ---------------------- RUN ----------------------

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        socketio.run(app, debug=True, use_reloader=False)
