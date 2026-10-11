FROM python:3.13-alpine

WORKDIR /app
COPY *.py .
RUN adduser -D -u 10001 bloc && mkdir /data && chown bloc:bloc /data
USER bloc
CMD ["python", "-u", "bridge.py"]
