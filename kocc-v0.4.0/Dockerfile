FROM registry.access.redhat.com/ubi9/python-312:9.6@sha256:d350329565030871c36d24c43533db869e892390fb47dc5c44512a32792dc623

WORKDIR /opt/app-root/src

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
