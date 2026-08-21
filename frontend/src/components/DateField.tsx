interface DateFieldProps {
  id?: string;
  value: string;
  onChange: (value: string) => void;
  today?: string;
  minYear?: number;
  maxYear?: number;
  disabled?: boolean;
}

/** 业务日用 YYYY-MM-DD；带时分的字符串只取前 10 位，不读浏览器时区。 */
export function datePart(value: string) {
  const match = /^(\d{4}-\d{2}-\d{2})/.exec(value || "");
  return match ? match[1] : "";
}

function pad(value: number) {
  return String(value).padStart(2, "0");
}

function splitDate(value: string) {
  const text = datePart(value);
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
  if (!match) return { year: 0, month: 0, day: 0 };
  return {
    year: Number(match[1]),
    month: Number(match[2]),
    day: Number(match[3]),
  };
}

function daysInMonth(year: number, month: number) {
  if (!year || !month) return 31;
  return new Date(Date.UTC(year, month, 0)).getUTCDate();
}

function joinDate(year: number, month: number, day: number) {
  if (!year || !month || !day) return "";
  const last = daysInMonth(year, month);
  return `${year}-${pad(month)}-${pad(Math.min(day, last))}`;
}

/** 年 / 月 / 日三个下拉，日历按 UTC 算，不读浏览器本地午夜。 */
export function DateField({
  id,
  value,
  onChange,
  today = "",
  minYear,
  maxYear,
  disabled,
}: DateFieldProps) {
  const current = splitDate(value);
  const todayYear = splitDate(today).year || current.year;
  const startYear = minYear ?? (todayYear ? todayYear - 1 : 2025);
  const endYear = maxYear ?? (todayYear ? todayYear + 1 : 2027);
  const years = [];
  for (let year = startYear; year <= endYear; year += 1) years.push(year);
  if (current.year && !years.includes(current.year)) {
    years.push(current.year);
    years.sort((left, right) => left - right);
  }
  const lastDay = daysInMonth(current.year || todayYear, current.month || 1);
  const days = [];
  for (let day = 1; day <= lastDay; day += 1) days.push(day);

  function patch(next: { year?: number; month?: number; day?: number }) {
    onChange(joinDate(
      next.year ?? current.year,
      next.month ?? current.month,
      next.day ?? current.day,
    ));
  }

  return (
    <div className="ymd-field" id={id}>
      <select
        aria-label="年"
        disabled={disabled}
        value={current.year || ""}
        onChange={(event) => patch({ year: Number(event.target.value) })}
      >
        {!current.year ? <option value="">年</option> : null}
        {years.map((year) => (
          <option key={year} value={year}>{year}年</option>
        ))}
      </select>
      <select
        aria-label="月"
        disabled={disabled}
        value={current.month || ""}
        onChange={(event) => patch({ month: Number(event.target.value) })}
      >
        {!current.month ? <option value="">月</option> : null}
        {Array.from({ length: 12 }, (_, index) => index + 1).map((month) => (
          <option key={month} value={month}>{month}月</option>
        ))}
      </select>
      <select
        aria-label="日"
        disabled={disabled}
        value={current.day && current.day <= lastDay ? current.day : ""}
        onChange={(event) => patch({ day: Number(event.target.value) })}
      >
        {!current.day ? <option value="">日</option> : null}
        {days.map((day) => (
          <option key={day} value={day}>{day}日</option>
        ))}
      </select>
    </div>
  );
}
