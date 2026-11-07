from flask import Flask
import redis

app = Flask(__name__)
r = redis.Redis(host='db_redis', port=6379, decode_responses=True)

@app.route('/')
def hello():
    try:
        count = r.incr('hits')
    except redis.ConnectionError:
        return "Database connection error", 500
    return f"Hello from modern Flask 3.1 + Redis 8! Counter: {count}\n"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)
