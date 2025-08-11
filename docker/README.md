# Monitor App Docker Setup 🐳

This directory contains Docker configuration files for running Monitor App in containers.

## Quick Start

### Using Docker Compose (Recommended)

1. **Build and start the application**:
   ```bash
   cd docker
   docker-compose up --build
   ```

2. **Access the application**:
   - Web UI: http://localhost:9990
   - API Documentation: http://localhost:9990/docs

3. **Stop the application**:
   ```bash
   docker-compose down
   ```

### Using Docker directly

1. **Build the image**:
   ```bash
   docker build -f docker/Dockerfile -t monitor-app .
   ```

2. **Run the container**:
   ```bash
   docker run -p 9990:9990 monitor-app
   ```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` | `0.0.0.0` | Host to bind the application |
| `PORT` | `9990` | Port to run the application |
| `DB_TYPE` | `sqlite` | Database type (`sqlite`, `mysql`, `postgresql`) |
| `PYTHONUNBUFFERED` | `1` | Python output buffering |

### Volume Mounts

The docker-compose.yml includes several volume mounts:

- **`csv_data`**: CSV files for data import
- **`db_data`**: SQLite database persistence
- **`./logs`**: Application logs

### Custom Configuration

To use custom configuration:

1. **Create a custom config file**:
   ```bash
   cp monitor_app/config/config.py my_config.py
   # Edit my_config.py with your settings
   ```

2. **Mount the config file**:
   ```yaml
   volumes:
     - ./my_config.py:/app/monitor_app/config/config.py
   ```

## Production Deployment

### With PostgreSQL

1. **Uncomment PostgreSQL service** in `docker-compose.yml`

2. **Update application configuration**:
   ```yaml
   environment:
     - DB_TYPE=postgresql
     - POSTGRES_HOST=postgres
     - POSTGRES_DB=monitor_app
     - POSTGRES_USER=postgres
     - POSTGRES_PASSWORD=your_secure_password
   ```

3. **Deploy**:
   ```bash
   docker-compose up -d
   ```

### With MySQL

1. **Uncomment MySQL service** in `docker-compose.yml`

2. **Update application configuration**:
   ```yaml
   environment:
     - DB_TYPE=mysql
     - MYSQL_HOST=mysql
     - MYSQL_DB=monitor_app
     - MYSQL_USER=root
     - MYSQL_PASSWORD=your_secure_password
   ```

### With Nginx (Reverse Proxy)

1. **Create nginx configuration**:
   ```bash
   mkdir nginx
   ```

2. **Create `nginx/nginx.conf`**:
   ```nginx
   events {
     worker_connections 1024;
   }
   
   http {
     upstream monitor_app {
       server monitor-app:9990;
     }
   
     server {
       listen 80;
       server_name your-domain.com;
   
       location / {
         proxy_pass http://monitor_app;
         proxy_set_header Host $host;
         proxy_set_header X-Real-IP $remote_addr;
         proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
         proxy_set_header X-Forwarded-Proto $scheme;
       }
     }
   }
   ```

3. **Uncomment nginx service** in `docker-compose.yml`

## Development

### Development with Live Reload

1. **Create development override**:
   ```yaml
   # docker-compose.override.yml
   version: '3.8'
   
   services:
     monitor-app:
       volumes:
         - ../monitor_app:/app/monitor_app
       command: ["python", "monitor_app/app.py", "runserver", "--host", "0.0.0.0", "--debug", "--csv"]
   ```

2. **Start development environment**:
   ```bash
   docker-compose up --build
   ```

### Running Tests in Container

```bash
# Build test image
docker build -f docker/Dockerfile.test -t monitor-app-test .

# Run tests
docker run --rm monitor-app-test
```

## Troubleshooting

### Common Issues

1. **Port already in use**:
   ```bash
   # Change port in docker-compose.yml
   ports:
     - "8080:9990"  # Use port 8080 instead
   ```

2. **Permission issues**:
   ```bash
   # Fix ownership
   sudo chown -R $USER:$USER ./csv_data ./db_data
   ```

3. **Database connection issues**:
   ```bash
   # Check database service logs
   docker-compose logs postgres
   docker-compose logs mysql
   ```

### Debugging

1. **Access container shell**:
   ```bash
   docker-compose exec monitor-app bash
   ```

2. **View application logs**:
   ```bash
   docker-compose logs monitor-app -f
   ```

3. **Check health status**:
   ```bash
   docker-compose ps
   ```

## Performance Optimization

### Multi-stage Build Benefits

The Dockerfile uses multi-stage builds to:
- Reduce final image size
- Exclude build dependencies from runtime
- Improve security by minimizing attack surface

### Resource Limits

Add resource limits in docker-compose.yml:

```yaml
services:
  monitor-app:
    deploy:
      resources:
        limits:
          memory: 512M
          cpus: '0.5'
        reservations:
          memory: 256M
          cpus: '0.25'
```

## Security Considerations

### Non-root User

The container runs as non-root user `appuser` for security.

### Network Isolation

Services communicate through isolated Docker networks.

### Secret Management

For production, use Docker secrets or external secret management:

```yaml
secrets:
  db_password:
    external: true

services:
  monitor-app:
    secrets:
      - db_password
```

## Monitoring

### Health Checks

Built-in health check verifies application availability:
- Endpoint: `/api/tables`
- Interval: 30 seconds
- Timeout: 10 seconds

### Logs

Access structured logs:
```bash
# Application logs
docker-compose logs monitor-app

# All services
docker-compose logs

# Follow logs
docker-compose logs -f
```

## Build Arguments

Customize build process:

```bash
# Use different Poetry version
docker build --build-arg POETRY_VERSION=1.7.0 -f docker/Dockerfile -t monitor-app .

# Use different Python version
docker build --build-arg PYTHON_VERSION=3.10 -f docker/Dockerfile -t monitor-app .
```

## Contributing

When contributing Docker-related changes:

1. **Test locally** with different configurations
2. **Update documentation** if adding new features
3. **Ensure security best practices** are followed
4. **Test multi-platform builds** (AMD64, ARM64)

## Examples

### Basic Usage
```bash
# Clone repository
git clone https://github.com/hardwork9047/monitor-app.git
cd monitor-app

# Start with sample data
docker-compose up --build
```

### Production Deployment
```bash
# Production with PostgreSQL
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d

# Scale application
docker-compose up --scale monitor-app=3 -d
```

### Development
```bash
# Development with live reload
docker-compose -f docker-compose.yml -f docker-compose.override.yml up --build
```