FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY chat_search ./chat_search
RUN pip install --no-cache-dir .
ENV CHAT_SEARCH_DATA=/data
VOLUME /data
EXPOSE 9180
CMD ["chat-search", "serve", "--host", "0.0.0.0", "--port", "9180"]
