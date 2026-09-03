// Native EventSource only supports GET, and every endpoint here needs a
// JSON body (the question, the plan_id to approve) — so this parses the
// SSE wire format by hand from a POSTed fetch()'s streamed response body,
// rather than reaching for an EventSource polyfill for a shape it was
// never going to support anyway.

export type SSEEvent = { event: string; data: any };

export async function* streamSSE(
  url: string,
  body: unknown,
  signal?: AbortSignal
): AsyncGenerator<SSEEvent> {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });

  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(`${resp.status} ${resp.statusText}${text ? `: ${text}` : ""}`);
  }
  if (!resp.body) {
    throw new Error("streaming response had no body — check the server sent Content-Type: text/event-stream");
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let boundary: number;
      // One SSE event ends at the first blank line — everything before
      // it is "event: x\ndata: y" (data may itself contain no newlines
      // here since json.dumps on the server never emits raw ones).
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        const rawEvent = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);

        let eventName = "message";
        let dataLine = "";
        for (const line of rawEvent.split("\n")) {
          if (line.startsWith("event:")) eventName = line.slice(6).trim();
          else if (line.startsWith("data:")) dataLine = line.slice(5).trim();
        }
        if (dataLine) {
          yield { event: eventName, data: JSON.parse(dataLine) };
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}
