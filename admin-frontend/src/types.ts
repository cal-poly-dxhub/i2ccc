export type SourceType = 'page' | 'gdrive' | 'panopto' | 'youtube' | 'pdf';

/** Source-level status: crawl and ingest are tracked separately. */
export type SourceStatus =
  | 'crawling'
  | 'crawl_success'
  | 'crawl_failed'
  | 'ingest_started'
  | 'ingest_complete'
  | 'ingest_failed'
  /** Rerun or delete is removing the source's content. */
  | 'cleaning_up'
  | 'cleanup_failed';

/** Child-level status, derived from the processed-files table. */
export type ChildStatus = 'pending' | 'ingested' | 'failed';

export interface SourceChild {
  id: string;
  title: string;
  url: string;
  type: SourceType;
  /** Id of the child (a page) this one was found on. Absent for items found directly on the source. */
  parentId?: string;
  /** 1 for items found directly on the source, 2 for items found on those pages, and so on. */
  level: number;
  status: ChildStatus;
}

export interface Source {
  id: string;
  title: string;
  url: string;
  type: SourceType;
  status: SourceStatus;
  addedAt: string; // ISO timestamp
  error?: string;
  /** Number of children across all pages. */
  childCount: number;
  /** First page of children, in tree order. */
  children: SourceChild[];
  /** Cursor for GET /api/sources/{id}/documents when there are more children. */
  childrenNextCursor?: string;
}

export interface SourcesPage {
  sources: Source[];
  /** Documents waiting for ingest, across all sources. */
  pendingCount: number;
  nextCursor?: string;
}

export interface DocumentsPage {
  documents: SourceChild[];
  total: number;
  nextCursor?: string;
}
