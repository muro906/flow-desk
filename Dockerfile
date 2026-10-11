FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

# Dependencies first, from pyproject alone, so code changes don't reinstall them.
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip python -c "\
import subprocess, sys, tomllib; \
deps = tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']; \
subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--timeout', '120', '--retries', '10', *deps])"

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
