# Network Check Tool

This package ships **two command-line tools**:

- **`netdiag`** — *local-first network & OS trouble isolation.* Runs a layered set
  of probes and a dependency-graph engine that reports **which layer is broken,
  why, and what to check next** — offline, without root. ([docs](docs/netdiag.md))
- **`network-checker`** — *ping monitoring with a web dashboard.* Periodic ping
  tests across multiple hosts, stored in SQLite and visualized with Plotly.

In short: `network-checker` tells you *that* something is down; `netdiag` tells
you *why*.

![Python Version](https://img.shields.io/badge/Python-3.10%2B-blue)
![License](https://img.shields.io/badge/License-MIT-yellow)
![PyPI](https://img.shields.io/pypi/v/network-check-tool)

## 🚀 Quick Start

### Installation from PyPI (Recommended)

Install the package directly from PyPI:

```bash
pip install network-check-tool
```

### Installation from Source

If you want to contribute or modify the code:

```bash
# Clone the repository
git clone https://github.com/mikawa-bushi/network-check-tool.git
cd network-check-tool

# Install with Poetry (recommended for development)
poetry install

# Or install with pip
pip install -e .
```

### First Steps

1. **Initialize the database:**
```bash
network-checker init
```

2. **Test a single host:**
```bash
network-checker ping google.com
```

3. **Start the web dashboard:**
```bash
network-checker run-server
```
Then open http://localhost:9991 in your browser.

## 🩺 `netdiag` — local-first trouble isolation (new)

The package now also ships **`netdiag`**, a *diagnosis* companion to the
monitoring tool. Where `network-checker` answers *"is the host reachable?"*,
`netdiag` answers **"which layer is broken, why, and what do I check next?"** —
fully offline, without root, on the standard library alone.

```bash
netdiag check                      # full layered sweep → root cause + next steps
netdiag watch                      # always-on: alert (file/JSONL) only when the verdict changes
netdiag explain dns                # learn what a check does + the manual commands
netdiag snapshot save ok           # later: netdiag snapshot diff ok → "what changed?"
```

It runs a dependency-ordered set of probes (L1 Wi-Fi → L2 ARP → L3 routing/
reachability → L4 ports/firewall → L7 DNS/HTTP/TLS, plus host/OS), then a
dependency-graph engine reports the **lowest failing layer** as the root cause
and folds everything above it into "consequences". Exit codes (`0` healthy / `1`
cause found / `2` warnings) make it script- and CI-friendly.

See **[docs/netdiag.md](docs/netdiag.md)** for the full design and command
reference.

## 📖 What is Network Check Tool?

Network Check Tool is a Python application designed for continuous network monitoring. It helps you:

- **Monitor network connectivity** to multiple hosts simultaneously
- **Track response times and packet loss** over time
- **Visualize network performance** through an interactive web dashboard
- **Set up automated monitoring** with customizable intervals
- **Access historical data** through CLI commands or REST API

Perfect for system administrators, DevOps engineers, and anyone who needs to monitor network reliability.

## ✨ Features

### 🎯 **Core Functionality**
- **Periodic Ping Monitoring**: Automated ping tests at configurable intervals
- **SQLite Database**: Persistent storage of ping results with timestamps
- **Web Dashboard**: Interactive visualization using Plotly charts
- **CLI Interface**: Comprehensive command-line tools for management
- **RESTful API**: JSON endpoints for data access and integration

### 📊 **Dashboard Features**
- Real-time response time charts
- Packet loss visualization
- Success rate statistics
- Multiple host monitoring
- Time range filtering (1 hour to 1 week)
- Auto-refresh every 30 seconds

### ⚡ **Advanced Features**
- Background scheduling with APScheduler
- Configurable ping timeouts and intervals
- Database indexing for performance
- Error handling and logging
- Development and production configurations

## 🛠️ Usage Examples

### Basic Monitoring

```bash
# Monitor a single host
network-checker ping google.com --count 5

# Start continuous monitoring of multiple hosts
network-checker start-monitoring google.com cloudflare.com 8.8.8.8 --interval 30

# Check recent results
network-checker status --host google.com --limit 10
```

### Web Dashboard

```bash
# Start the web server (default: binds to localhost only — http://localhost:9991)
network-checker run-server

# Expose on the network (opt-in). The dashboard and data API have NO built-in
# authentication, so only do this behind a reverse proxy that adds access control.
network-checker run-server --host 0.0.0.0 --port 8080
```

> **Security note**
> - `run-server` binds to `127.0.0.1` by default; `--host 0.0.0.0` (or any non-loopback
>   address) is an explicit opt-in and prints a warning.
> - There is no authentication on the dashboard or `/api/*`. For remote access, run behind a
>   reverse proxy (e.g. nginx) that handles TLS and authentication; do not expose the Flask
>   development server directly in production.
> - `--debug` (the Werkzeug interactive debugger) is refused on non-loopback hosts because it
>   allows arbitrary code execution. Debug only on `--host 127.0.0.1`.
> - Cross-origin access to `/api/*` is disabled unless you set `CORS_ORIGINS` (comma-separated
>   allowed origins).

### Data Management

```bash
# View all monitored hosts
network-checker status

# Clear all data (with confirmation)
network-checker clear
```

## 📋 CLI Commands Reference

| Command | Description | Example |
|---------|-------------|---------|
| `network-checker ping <host>` | Send ping tests to a specific host | `network-checker ping example.com --count 4` |
| `network-checker start-monitoring <hosts>` | Start continuous monitoring | `network-checker start-monitoring google.com --interval 60` |
| `network-checker run-server` | Start the web dashboard server | `network-checker run-server --port 9991` |
| `network-checker status` | Display recent ping results | `network-checker status --host google.com` |
| `network-checker init` | Initialize the database | `network-checker init` |
| `network-checker clear` | Clear all ping data | `network-checker clear` |

## 🌐 Web Dashboard

After starting the server with `network-checker run-server`, access the dashboard at http://localhost:9991.

### Dashboard Features
- **Host Selection**: Choose from monitored hosts
- **Time Range**: View data from 1 hour to 1 week
- **Statistics Cards**: Total pings, success rate, average response time, packet loss rate
- **Interactive Charts**: Response time trends with packet loss indicators
- **Auto-refresh**: Updates every 30 seconds

## 🔌 API Endpoints

The tool provides a REST API for integration with other tools:

### GET `/api/hosts`
Get list of monitored hosts
```bash
curl http://localhost:9991/api/hosts
# Returns: ["google.com", "cloudflare.com"]
```

### GET `/api/stats?host=<host>&hours=<hours>`
Get statistics for a specific host
```bash
curl "http://localhost:9991/api/stats?host=google.com&hours=24"
```

### GET `/api/ping_data?host=<host>&hours=<hours>`
Get raw ping data for visualization
```bash
curl "http://localhost:9991/api/ping_data?host=google.com&hours=1"
```

## ⚙️ Configuration

### Environment Variables

Customize behavior with environment variables:

```bash
# Database configuration
DATABASE_URL=sqlite:///network_check_tool.db
# SQLite-only: busy timeout (ms) applied alongside WAL journal mode. Ignored for
# non-SQLite backends. Raise it on slow/contended filesystems.
SQLITE_BUSY_TIMEOUT_MS=5000

# Ping settings
PING_TIMEOUT=4.0
DEFAULT_PING_INTERVAL=60

# Monitoring scheduler: worker-pool size. The default suits a handful of hosts;
# raise it when monitoring many hosts.
SCHEDULER_MAX_WORKERS=4

# Default hosts to monitor
DEFAULT_HOSTS=google.com,cloudflare.com

# Flask configuration
SECRET_KEY=your-secret-key
```

### Configuration Profiles

The application supports different configuration profiles:

- **Development**: Debug mode enabled, SQL logging
- **Production**: Optimized for production deployment

## 🏗️ Project Structure

```
network_check_tool/
├── __init__.py              # Package initialization
├── app.py                   # Flask application factory
├── cli.py                   # Command-line interface
├── models/
│   ├── __init__.py
│   └── ping_result.py       # SQLAlchemy model
├── services/
│   ├── __init__.py
│   ├── ping_service.py      # Ping execution service
│   └── scheduler_service.py # Background scheduling
├── views/
│   ├── __init__.py
│   └── dashboard.py         # Web API endpoints
├── templates/
│   └── dashboard.html       # Web dashboard UI
└── config/
    ├── __init__.py
    └── config.py            # Application configuration
```

## 📊 Database Schema

### ping_results table
- `id`: Primary key (INTEGER)
- `target_host`: Target hostname (VARCHAR(255), indexed)
- `timestamp`: Ping timestamp (DATETIME, indexed)
- `response_time`: Response time in milliseconds (FLOAT, nullable)
- `packet_loss`: Boolean indicating packet loss (BOOLEAN)
- `error_message`: Error details if ping failed (TEXT, nullable)

## 🔧 Development

### Development Setup

```bash
# Clone the repository
git clone https://github.com/mikawa-bushi/network-check-tool.git
cd network-check-tool

# Install with Poetry (recommended)
poetry install

# Or create a virtual environment and install with pip
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -e .
```

### Running Tests

```bash
poetry run pytest
# or
python -m pytest
```

### Code Formatting

```bash
poetry run black network_check_tool/
# or
black network_check_tool/
```

### Development Server

```bash
# Run with auto-reload for development
poetry run network-checker run-server --debug
```

## 🚀 Production Deployment

### With Docker (Recommended)

```dockerfile
# Dockerfile example
FROM python:3.10-slim
WORKDIR /app
COPY . .
RUN pip install network-check-tool
EXPOSE 9991
CMD ["network-checker", "run-server", "--host", "0.0.0.0", "--port", "9991"]
```

```bash
docker build -t network-check-tool .
docker run -p 9991:9991 network-check-tool
```

### Manual Deployment

```bash
# Install the package
pip install network-check-tool

# Initialize database
network-checker init

# Run with a production WSGI server
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:9991 "network_check_tool.app:create_app('production')"
```

## 📦 Dependencies

**Runtime Dependencies:**
- `flask` - Web framework
- `flask-sqlalchemy` - Database ORM
- `ping3` - Ping functionality
- `apscheduler` - Background scheduling
- `plotly` - Data visualization
- `pandas` - Data manipulation
- `flask-cors` - CORS support
- `click` - CLI framework
- `toml` - Configuration parsing

**Development Dependencies:**
- `pytest` - Testing framework
- `black` - Code formatting

## 🤝 Contributing

We welcome contributions! Please follow these steps:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Run tests (`pytest`)
5. Format code (`black .`)
6. Commit your changes (`git commit -m 'Add amazing feature'`)
7. Push to the branch (`git push origin feature/amazing-feature`)
8. Open a Pull Request

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🆘 Support

- 📖 [Documentation](docs/)
- 🐛 [Issue Tracker](https://github.com/mikawa-bushi/network-check-tool/issues)
- 💬 [Discussions](https://github.com/mikawa-bushi/network-check-tool/discussions)

## 🌟 Why Network Check Tool?

- **Easy to Use**: Simple CLI commands and intuitive web interface
- **Lightweight**: Minimal resource usage with SQLite database
- **Flexible**: Configurable monitoring intervals and multiple output formats
- **Reliable**: Robust error handling and comprehensive logging
- **Open Source**: MIT licensed and community-driven

---

**Network Check Tool** - Keep your network monitoring simple and effective! 🚀