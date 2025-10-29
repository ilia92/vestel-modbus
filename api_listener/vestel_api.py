from flask import Flask, request, jsonify, Response, send_from_directory
from flask_cors import CORS
import subprocess
import json
import os
from pathlib import Path

app = Flask(__name__)

# Enable CORS for all origins (can be restricted later for safety)
CORS(app, resources={r"/*": {"origins": "*"}})

CURRENT_DIR = Path(__file__).resolve().parent
VESTEL_SCRIPT = CURRENT_DIR.parent / "vestel.py"

# Ensure static directory exists
os.makedirs(os.path.join(os.path.dirname(__file__), 'static'), exist_ok=True)


@app.route('/')
def index():
    """Serve the main web interface."""
    return send_from_directory('.', 'index.html')


@app.route('/status', methods=['GET'])
def get_status():
    """Return JSON status from the Vestel charger exactly as the script outputs it."""
    try:
        result = subprocess.run(
            [VESTEL_SCRIPT, '--format=json'],
            capture_output=True, text=True, check=True
        )
        return Response(result.stdout, mimetype='application/json')
    except subprocess.CalledProcessError as e:
        return jsonify({'error': 'Failed to get status', 'details': e.stderr}), 500


@app.route('/metrics', methods=['GET'])
def get_metrics():
    """Return Prometheus metrics from the Vestel charger."""
    try:
        result = subprocess.run(
            [VESTEL_SCRIPT, '--format=prometheus'],
            capture_output=True, text=True, check=True
        )
        return result.stdout, 200, {'Content-Type': 'text/plain'}
    except subprocess.CalledProcessError as e:
        return jsonify({'error': 'Failed to read metrics', 'details': e.stderr}), 500


@app.route('/set-current', methods=['GET', 'POST'])
def set_current():
    """Set charging current through GET ?current=xx or POST {"current": xx}."""
    try:
        current = None

        if request.method == 'GET':
            current = request.args.get('current')
            if not current:
                return jsonify({'error': 'Missing current parameter'}), 400

        elif request.method == 'POST':
            data = request.get_json()
            if not data or 'current' not in data:
                return jsonify({'error': 'Missing current value in JSON body'}), 400
            current = data['current']

        # Apply the setting
        subprocess.run(
            [VESTEL_SCRIPT, '--format=prometheus', '--set-current', str(current)],
            capture_output=True, text=True, check=True
        )

        # Fetch updated status
        status_result = subprocess.run(
            [VESTEL_SCRIPT, '--format=json'],
            capture_output=True, text=True, check=True
        )

        try:
            status = json.loads(status_result.stdout)
            return jsonify({
                'success': True,
                'message': f'Current set to {current} successfully',
                'status': status
            }), 200
        except json.JSONDecodeError:
            return jsonify({
                'success': True,
                'message': f'Current set to {current} successfully'
            }), 200

    except subprocess.CalledProcessError as e:
        return jsonify({'error': 'Failed to set current', 'details': e.stderr}), 500


if __name__ == '__main__':
    # Bind on all interfaces so Grafana and local web pages can reach it
    app.run(host='0.0.0.0', port=5000, debug=False)
