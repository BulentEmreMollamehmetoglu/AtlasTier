FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ATLASTIER_DATA_DIR=/data
WORKDIR /srv/atlastier
COPY requirements.lock.txt .
RUN pip install --no-cache-dir -r requirements.lock.txt \
    && useradd --create-home --uid 10001 atlastier \
    && mkdir /data && chown atlastier:atlastier /data
COPY --chown=atlastier:atlastier . .
RUN chown atlastier:atlastier /srv/atlastier
USER atlastier
EXPOSE 8000
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
