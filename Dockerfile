FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg apt-transport-https unixodbc libgssapi-krb5-2 \
    && curl -fsSL -o /tmp/packages-microsoft-prod.deb https://packages.microsoft.com/config/debian/12/packages-microsoft-prod.deb \
    && dpkg -i /tmp/packages-microsoft-prod.deb \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && rm -rf /var/lib/apt/lists/* /tmp/packages-microsoft-prod.deb \
    && driver="$(find /opt/microsoft/msodbcsql18/lib64 -name 'libmsodbcsql-18*.so*' | head -n 1)" \
    && test -n "$driver" \
    && if ldd "$driver" | grep 'not found'; then exit 1; fi \
    && printf '%s\n' \
        '[ODBC Driver 18 for SQL Server]' \
        'Description=Microsoft ODBC Driver 18 for SQL Server' \
        "Driver=${driver}" \
        'UsageCount=1' \
        > /etc/odbcinst.ini \
    && odbcinst -q -d | grep 'ODBC Driver 18 for SQL Server'

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}"]
