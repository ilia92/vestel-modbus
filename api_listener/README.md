# Vestel Charger API Service

A simple API wrapper around the Vestel reader/writer script with a web interface for monitoring and control.

## Prerequisites

- Python 3.6+
- Flask (`pip install flask`)
- The original `vestel.py` script in the parent directory

## Setup

1. Install Flask if you don't have it:
   ```
   pip install flask
   ```

2. Ensure the `vestel.py` script is executable:
   ```
   chmod +x ../vestel.py
   ```

3. Run the API server:
   ```
   python vestel_api.py
   ```

4. Access the web interface by opening a browser and navigating to:
   ```
   http://localhost:5000
   ```

## Web Interface

The web interface provides a user-friendly dashboard to:
- Monitor the connection status of your Vestel charger
- View the current charging status
- See real-time power and energy consumption
- Control the charging current with a slider (6A-32A)

The interface automatically refreshes every 30 seconds and provides visual feedback when settings are applied.

## API Endpoints

### Web Interface

- **URL**: `/`
- **Method**: `GET`
- **Response**: HTML page with the control interface

### Get Status

Retrieves the exact JSON output from the Vestel charger.

- **URL**: `/status`
- **Method**: `GET`
- **Response**: Raw JSON exactly as returned by `../vestel.py --format=json`

Example:
```
curl http://localhost:5000/status
```

### Get Metrics

Retrieves all metrics from the Vestel charger in Prometheus format.

- **URL**: `/metrics`
- **Method**: `GET`
- **Response**: Plain text in Prometheus format

Example:
```
curl http://localhost:5000/metrics
```

### Set Current

Sets the current for the Vestel charger. Supports both GET (URL parameter) and POST (JSON body).

#### Using GET with URL parameter:

- **URL**: `/set-current?current=16`
- **Method**: `GET`
- **Response**: JSON with success message and updated status

Example:
```
curl http://localhost:5000/set-current?current=16
```

#### Using POST with JSON body:

- **URL**: `/set-current`
- **Method**: `POST`
- **Content-Type**: `application/json`
- **Request Body**:
  ```json
  {
    "current": 16
  }
  ```
- **Response**: JSON with success message and updated status

Example:
```
curl -X POST -H "Content-Type: application/json" -d '{"current": 16}' http://localhost:5000/set-current
```

## Running as a Service

To run this as a persistent service, you can use systemd. Create a file `/etc/systemd/system/vestel-api.service`:

```
[Unit]
Description=Vestel Charger API Service
After=network.target

[Service]
User=your_username
WorkingDirectory=/path/to/directory
ExecStart=/usr/bin/python3 /path/to/directory/vestel_api.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Then enable and start the service:
```
sudo systemctl enable vestel-api.service
sudo systemctl start vestel-api.service
```
