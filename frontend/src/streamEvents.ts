export type ServerEventData = Record<string, unknown>;

export function dispatchServerEventPacket(
  packet: string,
  onEvent: (event: string, data: ServerEventData) => void,
): void {
  let event = "message";
  let dataText = "";
  for (const line of packet.split(/\r?\n/)) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) dataText += line.slice(5).trim();
  }
  if (!dataText) return;

  let data: unknown;
  try {
    data = JSON.parse(dataText);
  } catch {
    return;
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) return;
  onEvent(event, data as ServerEventData);
}
