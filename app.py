import os
from flask import Flask, request, jsonify, session, render_template, abort
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect, generate_csrf
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from datetime import timedelta
import bleach
import secrets
import anthropic

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///comments.db'
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or 'fallback-secret-key'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=1)
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['WTF_CSRF_TIME_LIMIT'] = None

db = SQLAlchemy(app)
csrf = CSRFProtect(app)
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

# Anthropic API setup
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
if not ANTHROPIC_API_KEY:
    raise ValueError("ANTHROPIC_API_KEY environment variable is not set")
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

class Comment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    original = db.Column(db.String(500), nullable=False)
    rewritten = db.Column(db.String(500), nullable=False)
    votes = db.Column(db.Integer, default=0)
    session_id = db.Column(db.String(100), nullable=False)
    answered = db.Column(db.Boolean, default=False)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/check_login')
def check_login():
    return jsonify({
        'logged_in': 'user_id' in session,
        'csrf_token': generate_csrf()
    })

@app.route('/login', methods=['POST'])
@limiter.limit("5 per minute")
def login():
    session.clear()
    session.permanent = True
    session['user_id'] = secrets.token_urlsafe(16)
    return jsonify({"success": True, "message": "Logged in successfully"})

@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({"success": True, "message": "Logged out successfully"})

@app.route('/get_comments')
def get_comments():
    comments = Comment.query.order_by(Comment.answered, Comment.votes.desc()).all()
    return jsonify({
        'comments': [{
            'id': comment.id,
            'rewritten': comment.rewritten,
            'votes': comment.votes,
            'answered': comment.answered
        } for comment in comments]
    })

@app.route('/submit_comment', methods=['POST'])
@limiter.limit("10 per minute")
def submit_comment():
    if 'user_id' not in session:
        abort(401)
    
    data = request.get_json()
    original_comment = data.get('comment')
    if not original_comment:
        return jsonify({"success": False, "message": "Comment is required"}), 400
    
    sanitized_comment = bleach.clean(original_comment)
    rewritten_comment = rewrite_comment(sanitized_comment)
    
    try:
        new_comment = Comment(original=sanitized_comment, rewritten=rewritten_comment, session_id=session['user_id'])
        db.session.add(new_comment)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "message": "Error saving comment"}), 500
    
    return jsonify({
        "success": True, 
        "id": new_comment.id, 
        "rewritten": rewritten_comment,
        "votes": new_comment.votes,
        "answered": new_comment.answered
    })

@app.route('/vote', methods=['POST'])
@limiter.limit("20 per minute")
def vote_comment():
    if 'user_id' not in session:
        abort(401)
    
    data = request.get_json()
    comment_id = data.get('id')
    try:
        comment = Comment.query.get(comment_id)
        if comment:
            comment.votes += 1
            db.session.commit()
            return jsonify({"success": True, "votes": comment.votes})
        return jsonify({"success": False, "message": "Comment not found"}), 404
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "message": "Error updating vote"}), 500

@app.route('/remove', methods=['POST'])
def remove_comment():
    if 'user_id' not in session:
        abort(401)
    
    data = request.get_json()
    comment_id = data.get('id')
    try:
        comment = Comment.query.get(comment_id)
        if comment and comment.session_id == session['user_id']:
            db.session.delete(comment)
            db.session.commit()
            return jsonify({"success": True})
        return jsonify({"success": False, "message": "Comment not found or not authorized"}), 404
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "message": "Error removing comment"}), 500

@app.route('/answer', methods=['POST'])
def answer_comment():
    if 'user_id' not in session:
        abort(401)
    
    data = request.get_json()
    comment_id = data.get('id')
    try:
        comment = Comment.query.get(comment_id)
        if comment:
            comment.answered = True
            db.session.commit()
            return jsonify({"success": True, "answered": comment.answered})
        return jsonify({"success": False, "message": "Comment not found"}), 404
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "message": "Error updating comment status"}), 500

def rewrite_comment(comment):
    try:
        message = anthropic_client.messages.create(
            model="claude-3-5-sonnet-20240620",
            max_tokens=1000,
            temperature=0.7,
            system="You are an assistant that rewrites comments in a more professional tone.",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"Rewrite the following comment in a more professional tone:\n\n{comment}"
                        }
                    ]
                }
            ]
        )
        return message.content[0].text
    except Exception as e:
        print(f"Error calling Anthropic API: {e}")
        return f"Error: Unable to rewrite comment. Please try again later."

@app.errorhandler(401)
def unauthorized(error):
    return jsonify({"success": False, "message": "Not authenticated"}), 401

@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({"success": False, "message": "Rate limit exceeded"}), 429

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=5000, debug=False)