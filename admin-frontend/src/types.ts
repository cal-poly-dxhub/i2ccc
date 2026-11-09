export type SourceType = 'page' | 'gdrive' | 'panopto' | 'youtube' | 'pdf';

/** Source-level status: crawl and ingest are tracked separately. */
export type SourceStatus =
  | 'crawling'
  | 'crawl_success'
  | 'crawl_failed'
  | 'ingest_started'
  | 'ingest_complete'
  | 'ingest_failed';

/** Child-level status, derived from the processed-files table. */
export type ChildStatus = 'pending' | 'ingested' | 'failed';

export interface SourceChild {
  id: string;
  title: string;
  url: string;
  type: SourceType;
  /** Id of the child (a page) this one was found on. Absent for items found directly on the source. */
  parentId?: string;
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
  children?: SourceChild[];
}
