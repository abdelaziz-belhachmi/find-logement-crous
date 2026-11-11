from dotenv import load_dotenv
load_dotenv(override=True)  # must run before importing scraper (it imports config, which reads env vars)

from flask import Flask, render_template, request, redirect, url_for, flash
from flask_socketio import SocketIO, emit
import threading
import time
import logging
from models import db, Alert, AlertLog
from scraper import check_for_results, clear_seen
import json
import os
from datetime import datetime


app = Flask(__name__)
SECRET_KEY = os.getenv('SECRET_KEY')
assert SECRET_KEY, 'SECRET_KEY environment variable not set'

# Get the PostgreSQL connection string from the environment variable
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///alerts.db')
assert DATABASE_URL, 'DATABASE_URL environment variable not set'

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = SECRET_KEY


db.init_app(app)

with app.app_context():
    db.create_all()  # Create the database tables

socketio = SocketIO(app)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Track scraping threads
scraping_threads = {}


def log_message(message, alert=None):
    """Helper function to format and log messages with additional details."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    alert_name = f"[Alert: {alert.name}]" if alert else ""
    full_message = f"{timestamp} {alert_name} {message}"
    logger.info(full_message)
    socketio.emit('log', {'message': full_message})


def start_scraping(alert_id):
    """Scraping logic for a specific alert."""
    with app.app_context():
        alert = Alert.query.get(alert_id)
        if not alert:
            log_message(f"Alert with ID {alert_id} no longer exists.")
            return

        log_message(f"Started scraping for {alert.city} with max price {alert.price}", alert)

        while alert.status:
            try:
                found = check_for_results(alert)
            except Exception:
                logger.exception('Unexpected error in check_for_results')
                found = False

            if found:
                log_message(f"New listings found for {alert.city}, email sent. Waiting for {alert.interval} seconds.", alert)
            else:
                log_message(f"No new listings for {alert.city}. Waiting for {alert.interval} seconds before next check.", alert)

            time.sleep(alert.interval)
            # Refresh the alert status from the database
            db.session.refresh(alert)

        log_message(f"Scraping stopped for {alert.city}.", alert)

        # Remove the thread from tracking after completion
        if alert_id in scraping_threads:
            del scraping_threads[alert_id]


def manage_alerts():
    """Background task that checks the status of all alerts and starts/stops scraping."""
    logger.info('Alert manager started.')
    last_active = None
    while True:
        try:
            with app.app_context():
                alerts = Alert.query.filter_by(status=True).all()
                active_ids = sorted(a.id for a in alerts)
                if active_ids != last_active:
                    total = Alert.query.count()
                    logger.info(f'Alert manager: {len(active_ids)} active alert(s) {active_ids} out of {total} total.')
                    last_active = active_ids
                for alert in alerts:
                    if alert.id not in scraping_threads or not scraping_threads[alert.id].is_alive():
                        thread = threading.Thread(target=start_scraping, args=(alert.id,), daemon=True)
                        thread.start()
                        scraping_threads[alert.id] = thread
        except Exception:
            logger.exception('manage_alerts error')

        time.sleep(5)  # Check every 5 seconds for active alerts


# Load meta.json to pass to templates
def get_meta_data():
    with open('static/meta.json') as meta_file:
        return json.load(meta_file)


@app.route('/')
def index():
    meta = get_meta_data()
    alerts = Alert.query.all()
    return render_template('index.html', alerts=alerts, meta=meta)


@app.route('/alerts/new', methods=['GET', 'POST'])
def create_alert():
    meta = get_meta_data()
    if request.method == 'POST':
        name = request.form['city']  # Save the city as the name of the alert
        city = request.form['city']
        assert request.form['bounds'], 'Bounds cannot be empty'
        bounds = request.form['bounds']  # Save the bounds separately
        price = float(request.form['price'])
        emails = request.form['emails']
        interval = int(request.form['interval'])  # Refresh duration
        status = request.form.get('status') == 'on'  # Toggle

        alert = Alert(name=name, city=city, bounds=bounds, price=price, emails=emails, interval=interval, status=status)
        db.session.add(alert)
        db.session.commit()

        flash('Alert created successfully!')
        return redirect(url_for('index'))

    return render_template('create_alert.html', meta=meta)


@app.route('/alerts/<int:alert_id>/edit', methods=['GET', 'POST'])
def edit_alert(alert_id):
    meta = get_meta_data()
    alert = Alert.query.get_or_404(alert_id)

    if request.method == 'POST':
        alert.name = request.form['city']  # Update the alert name
        alert.city = request.form['city']  # Keep city as it is
        assert request.form['bounds'], 'Bounds cannot be empty'
        alert.bounds = request.form['bounds']  # Update bounds
        alert.price = float(request.form['price'])
        alert.emails = request.form['emails']
        alert.interval = int(request.form['interval'])
        alert.status = request.form.get('status') == 'on'
        db.session.commit()

        flash('Alert updated successfully!')
        return redirect(url_for('index'))

    logs = AlertLog.query.filter_by(alert_id=alert.id).all()
    return render_template('edit_alert.html', alert=alert, logs=logs, meta=meta)


@app.route('/alerts/<int:alert_id>/toggle')
def toggle_alert_status(alert_id):
    alert = Alert.query.get_or_404(alert_id)
    alert.status = not alert.status
    db.session.commit()
    action = "activated" if alert.status else "deactivated"
    log_message(f"Alert for {alert.city} {action}.", alert)
    return redirect(url_for('index'))


@app.route('/alerts/<int:alert_id>/delete', methods=['POST', 'GET'])
def delete_alert(alert_id):
    with app.app_context():
        alert = Alert.query.get_or_404(alert_id)

        # Check if the alert is still running and stop the thread
        if alert_id in scraping_threads and scraping_threads[alert_id].is_alive():
            log_message(f"Stopping scraping for {alert.city} before deletion.", alert)
            # The thread will check the alert's status and exit
            alert.status = False
            db.session.commit()
            scraping_threads[alert_id].join()  # Wait for the thread to finish
            scraping_threads.pop(alert_id, None)  # Remove from the thread tracking dictionary

        log_message(f"Alert for {alert.city} is being deleted.", alert)
        clear_seen(alert_id)
        db.session.delete(alert)
        db.session.commit()
        flash('Alert deleted successfully!')
    return redirect(url_for('index'))


@socketio.on('start_scraping')
def handle_start_scraping(data):
    """Activate the alert; manage_alerts() picks it up within 5 seconds."""
    with app.app_context():
        alert = Alert.query.get(data['alert_id'])
        if alert:
            alert.status = True
            db.session.commit()
            log_message(f"Alert for {alert.city} activated.", alert)


@socketio.on('stop_scraping')
def handle_stop_scraping(data=None):
    """Deactivate the alert; its thread exits after the current wait."""
    with app.app_context():
        alert_id = (data or {}).get('alert_id')
        alert = Alert.query.get(alert_id) if alert_id else None
        if alert:
            alert.status = False
            db.session.commit()
            log_message(f"Alert for {alert.city} deactivated.", alert)
        else:
            logger.info('stop_scraping received without a valid alert_id.')


if __name__ == '__main__':
    # Service / normal run: no reloader, debug off.
    # For development set FLASK_DEBUG=1 (reloader on; the manager then starts only in
    # the child process that serves requests, otherwise alerts would run twice).
    debug = os.environ.get('FLASK_DEBUG') == '1'
    if not debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        threading.Thread(target=manage_alerts, daemon=True).start()
    socketio.run(app, host='127.0.0.1', port=5000, debug=debug, use_reloader=debug,
                 allow_unsafe_werkzeug=True)