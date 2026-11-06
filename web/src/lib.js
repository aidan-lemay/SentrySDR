// API access and display formatting. All URLs are relative (see vite.config.js).

export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === 'string' ? detail : 'Please check the values you entered.');
    this.status = status;
    this.detail = detail;
  }
}

let onSignedOut = () => {};
export function setSignedOutHandler(handler) {
  onSignedOut = handler;
}

export async function api(path, { method = 'GET', body, headers = {} } = {}) {
  let response;
  try {
    response = await fetch(`api/${path}`, {
      method,
      credentials: 'same-origin',
      headers: {
        'X-SentrySDR': '1',
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
        ...headers,
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, 'Can’t reach the server. Check your connection.');
  }
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!response.ok) {
    if (response.status === 401 && data?.detail === 'Not signed in') onSignedOut();
    throw new ApiError(response.status, data?.detail ?? `Request failed (${response.status})`);
  }
  return data;
}

export const MODULATIONS = [
  { value: 'nfm', label: 'NFM (narrow FM)' },
  { value: 'am', label: 'AM (airband)' },
  { value: 'wfm', label: 'WFM (broadcast)' },
  { value: 'p25p1', label: 'P25 Phase 1' },
  { value: 'p25p2', label: 'P25 Phase 2' },
  { value: 'dmr', label: 'DMR' },
  { value: 'nxdn48', label: 'NXDN48' },
  { value: 'nxdn96', label: 'NXDN96' },
  { value: 'dstar', label: 'D-STAR' },
  { value: 'ysf', label: 'YSF (Fusion)' },
];

export const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

export const formatMHz = (hz) => (hz == null ? '' : `${(hz / 1e6).toFixed(4).replace(/0{1,2}$/, '')} MHz`);
const plainMHz = (hz) => String(+(hz / 1e6).toFixed(4));
export const formatRanges = (ranges) => (ranges?.length
  ? `${ranges.map(([low, high]) => `${plainMHz(low)} to ${plainMHz(high)}`).join(', ')} MHz`
  : 'Not set');
// An antenna with no ranges entered isn't judged, as on the server.
export const antennaCovers = (ranges, hz) => !ranges?.length || ranges.some(([low, high]) => low <= hz && hz <= high);
export const ANTENNA_UNITS = [['in', 'inches'], ['ft', 'feet'], ['cm', 'cm'], ['m', 'metres']];
export const formatTime = (epoch) => (epoch ? new Date(epoch * 1000).toLocaleString() : 'None');

export function formatDuration(seconds) {
  const total = Math.round(seconds ?? 0);
  const minutes = Math.floor(total / 60);
  return minutes ? `${minutes}m ${String(total % 60).padStart(2, '0')}s` : `${total}s`;
}

export function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1048576).toFixed(1)} MB`;
}

export function formatSchedule(recorder) {
  if (!recorder.schedule_days) return 'Always on';
  const days = recorder.schedule_days.map((day) => DAYS[day]).join(', ');
  return `${days} · ${recorder.schedule_start}–${recorder.schedule_end}`;
}

// Where new gain boxes start. Tested on air on R820T and R828D sticks. Empty means AGC.
export const DEFAULT_GAIN_DB = 40.2;

export const STATE_LABELS = {
  listening: 'Listening', idle: 'Idle', error: 'Fault', offline: 'Offline', measuring: 'Measuring noise',
};
