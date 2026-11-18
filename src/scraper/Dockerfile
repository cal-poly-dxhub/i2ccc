# The scraper's Lambda functions: one image; each function picks its step with its CMD
# (i2cc_scraper.aws.handlers.crawl / plan / ingest / report). Build with this directory
# (src/scraper) as the context:  docker build src/scraper
FROM public.ecr.aws/lambda/python:3.13

# yt-dlp needs a JavaScript runtime to get YouTube's formats.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

COPY requirements.txt pyproject.toml README.md /build/
COPY i2cc_scraper /build/i2cc_scraper
RUN pip install --no-cache-dir -r /build/requirements.txt \
 && pip install --no-cache-dir --no-deps /build \
 && rm -rf /build

# Only /tmp is writable in Lambda: caches go there.
ENV HOME=/tmp XDG_CACHE_HOME=/tmp/.cache DENO_DIR=/tmp/.deno

CMD ["i2cc_scraper.aws.handlers.crawl"]
