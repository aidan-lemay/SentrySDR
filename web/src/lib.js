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
export const formatTime = (epoch) => (epoch ? new Date(epoch * 1000).toLocaleString() : 'Never');

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

// A moment as the master's time zone sees it, since schedules run on the master's clock.
export function inZone(epoch, timeZone, options) {
  return new Intl.DateTimeFormat(undefined, { timeZone: timeZone || undefined, ...options }).format(new Date(epoch * 1000));
}

// The value a datetime-local input shows for a moment, in the master's time zone.
export function zonedInputValue(epoch, timeZone) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-CA', {
    timeZone: timeZone || undefined, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(new Date(epoch * 1000)).map((p) => [p.type, p.value]));
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

export function formatSchedule(recorder, timeZone) {
  if (!recorder.schedule_days) return 'Always on';
  if (recorder.repeat === 'once') {
    const day = inZone(recorder.once_start, timeZone, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
    const time = (epoch) => inZone(epoch, timeZone, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
    const sameDay = recorder.once_end - recorder.once_start < 86400
      && inZone(recorder.once_start, timeZone, { dateStyle: 'short' }) === inZone(recorder.once_end - 1, timeZone, { dateStyle: 'short' });
    return sameDay ? `${day}, ${time(recorder.once_start)}–${time(recorder.once_end)}`
      : `${day} ${time(recorder.once_start)} to ${inZone(recorder.once_end, timeZone, { weekday: 'short', day: 'numeric', month: 'short' })} ${time(recorder.once_end)}`;
  }
  const days = recorder.schedule_days.map((day) => DAYS[day]).join(', ');
  return `${days} · ${recorder.schedule_start}–${recorder.schedule_end}`;
}

// Where new gain boxes start. Tested on air on R820T and R828D sticks. Empty means AGC.
export const DEFAULT_GAIN_DB = 40.2;

export const STATE_LABELS = {
  listening: 'Listening', idle: 'Idle', error: 'Fault', offline: 'Offline', measuring: 'Measuring noise',
  detecting: 'Detecting tuner',
};

// What a stick can do. Keep in step with sentrysdr/capabilities.py.
export const RECORDED_USES = [['voice', 'Analog voice'], ['digital', 'Digital voice'], ['adsb', 'ADS-B'], ['ais', 'AIS'], ['acars', 'ACARS']];
export const OTHER_USES = [['broadcast_fm', 'Broadcast FM']];
const ANALOG_MODES = ['nfm', 'am', 'wfm'];
export const useLabel = (key) => [...RECORDED_USES, ...OTHER_USES].find(([id]) => id === key)?.[1] ?? key;

// The step the stick will really use when a gain isn't one of its steps, else null.
export function nearestStep(steps, gain) {
  if (!steps?.length || gain === '' || gain === null || gain === undefined) return null;
  const value = Number(gain);
  const nearest = steps.reduce((best, step) => (Math.abs(step - value) < Math.abs(best - value) ? step : best));
  return Math.abs(nearest - value) <= 0.05 ? null : nearest;
}

function neededUses(target) {
  if (target.kind === 'rtltcp') return [];  // raw samples: any stick can serve them
  if (['adsb', 'ais', 'acars'].includes(target.kind)) return [target.kind];
  if (target.modulation === 'wfm') return ['voice', 'broadcast_fm'];
  if (ANALOG_MODES.includes(target.modulation)) return ['voice'];
  return ['digital'];
}

// Short reasons a stick may not suit a target, for marking pickers. Unset values aren't judged.
export function stickProblems(source, target) {
  if (!source || !target) return [];
  const found = [];
  const hz = target.frequency_hz;
  if ((source.cap_min_hz != null && hz < source.cap_min_hz) || (source.cap_max_hz != null && hz > source.cap_max_hz)) found.push('outside stick range');
  const needed = neededUses(target);
  if (source.uses?.length && needed.length && !needed.some((use) => source.uses.includes(use))) found.push('stick not set up for this');
  if (!antennaCovers(source.antenna_ranges, hz)) found.push('outside antenna range');
  return found;
}
// Short, so a long target name still fits in a dropdown. The hint below it gives the reasons.
export const problemNote = (source, target) => (stickProblems(source, target).length ? ' (may not suit)' : '');
export const formatRange = (low, high) => (low == null && high == null ? 'Not set' : `${low == null ? '?' : plainMHz(low)} to ${high == null ? '?' : plainMHz(high)} MHz`);

// Feeding other services. Keep in step with sentrysdr/feeds.py.
export const ADSB_AGGREGATORS = [['adsbx', 'ADS-B Exchange'], ['airplanes_live', 'airplanes.live'], ['adsb_lol', 'adsb.lol'], ['adsb_fi', 'adsb.fi']];
export const AIS_SERVICES = [['marinetraffic', 'MarineTraffic'], ['aishub', 'AISHub'], ['vesselfinder', 'VesselFinder']];
export const feedLabel = (key) => [...ADSB_AGGREGATORS, ...AIS_SERVICES, ['community', 'aiscatcher.org']].find(([id]) => id === key)?.[1] ?? key;
