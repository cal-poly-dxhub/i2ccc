"""Crawl Internet2 web pages and download what they link to, for RAG ingestion.

    scraper     find pages, their text, and their video/document links (crawl_for_ingest)
    downloader  download those into a Store, with a metadata Record each (download_all, ingest)
    store       where content and Records are kept (LocalStore; an S3 store implements the same)
    config      settings (Config), from code or I2CC_* environment variables
    netguard    start-URL allowlist and private-address blocking
    drive_api   Google Drive API access with a service account
"""
__version__ = '0.2.0'
