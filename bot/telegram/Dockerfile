FROM python:3.13-alpine

WORKDIR /app
COPY bloc_bridge ./bloc_bridge
RUN adduser -D -u 10001 bloc && mkdir /data && chown bloc:bloc /data
USER bloc
CMD ["python", "-u", "-m", "bloc_bridge"]
