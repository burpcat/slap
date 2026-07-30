// Thin typed fetch helpers over /api/* (proxied to Flask in dev, same-origin
// in the built app). Every GET returns the parsed JSON body; every POST
// sends a JSON body and returns the parsed JSON body, throwing an ApiError
// (carrying the server's {error} message when present) on a non-2xx status
// so react-query's error state/onError always has a readable message.

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function handle<T>(res: Response): Promise<T> {
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    // no/invalid JSON body — fall through to status-based error below
  }
  if (!res.ok) {
    const message =
      body && typeof body === 'object' && 'error' in (body as Record<string, unknown>)
        ? String((body as Record<string, unknown>).error)
        : `request failed with status ${res.status}`;
    throw new ApiError(message, res.status);
  }
  return body as T;
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { Accept: 'application/json' } });
  return handle<T>(res);
}

export async function apiPost<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  return handle<T>(res);
}
