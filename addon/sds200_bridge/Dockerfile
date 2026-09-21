ARG BUILD_FROM
FROM $BUILD_FROM

# sqlite-libs is pulled in by python3 anyway, but the receive history is a
# real SQLite database now (app/history.py) rather than a JSON file, so the
# dependency is named here rather than left to look incidental.
RUN apk add --no-cache python3 py3-pip ffmpeg sqlite-libs

COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY app /app
COPY run.sh /
RUN chmod a+x /run.sh

WORKDIR /app
CMD [ "/run.sh" ]
