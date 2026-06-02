'use client';

/**
 * Lightweight markdown renderer for AI zoning responses.
 * Handles: **bold**, * / • / - bullets, numbered lists,
 * ⚠️ warning lines, 📄 Source lines (with clickable section links),
 * ## headings, blank-line spacing.
 * No external deps — pure JSX.
 */

const BYLAW_BASE = 'https://www.toronto.ca/zoning/bylaw_amendments/ZBL_NewProvision_Chapter';

/**
 * Convert a section ID like "10.5.40.11", "150.8", "900.3.10(1065)" to a URL.
 *
 * Toronto's website uses per-subsection HTML files:
 *   1 part  (X)      → ChapterX.htm
 *   2 parts (X.Y)    → ChapterX_Y.htm           (the file IS the section — no anchor)
 *   3+ parts (X.Y.Z) → ChapterX_Y.htm#X.Y.Z     (subdocument + anchor)
 */
function sectionUrl(raw) {
  // Strip all parenthetical suffixes: (1065), (1)(A), etc.
  const clean = raw.trim().replace(/\([^)]*\)/g, '').trim();
  const parts = clean.split('.');
  if (!parts[0] || isNaN(Number(parts[0]))) return null;
  if (parts.length === 1) {
    return `${BYLAW_BASE}${parts[0]}.htm`;
  }
  if (parts.length === 2) {
    return `${BYLAW_BASE}${parts[0]}_${parts[1]}.htm`;
  }
  // 3+ parts
  return `${BYLAW_BASE}${parts[0]}_${parts[1]}.htm#${clean}`;
}

// Alias used by parseInline — same logic, single implementation
const sectionIdToUrl = sectionUrl;

/** Render the 📄 Source line with each section ID as a clickable link */
function SourceLine({ text }) {
  // text like: "📄 Source: 10.5.40.11, 10.10.40.10(1)(A), 995.20"
  const afterEmoji = text.replace(/^📄\s*/, '').trim();
  const label      = afterEmoji.startsWith('Source:')
    ? 'Source'
    : afterEmoji.split(':')[0] || 'Source';
  const rest       = afterEmoji.includes(':')
    ? afterEmoji.slice(afterEmoji.indexOf(':') + 1).trim()
    : afterEmoji;

  // Split on commas
  const ids = rest.split(',').map(s => s.trim()).filter(Boolean);

  return (
    <div className="md-source">
      <span className="md-source-emoji">📄</span>
      <span className="md-source-label">{label}:</span>
      <span className="md-source-links">
        {ids.map((id, i) => {
          const url = sectionUrl(id);
          return (
            <span key={i}>
              {i > 0 && <span className="md-source-sep">, </span>}
              {url ? (
                <a href={url} target="_blank" rel="noreferrer" className="md-source-link">
                  {id}
                </a>
              ) : (
                <span className="md-source-plain">{id}</span>
              )}
            </span>
          );
        })}
      </span>
    </div>
  );
}

/**
 * Tokenise a line into typed tokens.
 * Priority (first match wins):
 *   1. [display](url)       — markdown link
 *   2. `code`               — backtick inline code / section ref
 *   3. [Section X.X.X.X…]  — bare section citation in square brackets
 *   4. https?://…           — raw URL (auto-link)
 *   5. **bold**             — bold
 */
function parseInline(text) {
  const RE = new RegExp(
    // 1. Markdown link  [display](url)
    '(\\[([^\\]\\n]{1,200})\\]\\((https?:\\/\\/[^)\\s]{4,500})\\))' +
    // 2. Backtick code  `…`
    '|(`[^`\\n]{1,200}`)' +
    // 3. Bare section citation  [Section 10.5.40.10] or [10.5.40.10(1)(A)]
    '|(\\[(?:Section\\s+)?([\\d]+(?:\\.[\\d]+)+(?:\\([^)\\n]{1,20}\\))*)\\])' +
    // 4. Raw URL  https://… (terminated by whitespace, ), ], or end)
    '|(https?:\\/\\/[^\\s)\\]>,"\']{6,500})' +
    // 5. Bold  **…**
    '|(\\*\\*[^*\\n]{1,300}\\*\\*)',
    'g'
  );

  const tokens = [];
  let cursor = 0, m;

  while ((m = RE.exec(text)) !== null) {
    if (m.index > cursor) tokens.push({ t: 'text', v: text.slice(cursor, m.index) });

    if (m[1]) {
      // Markdown link
      const label = m[2].startsWith('http')
        ? (m[3].includes('#') ? m[3].split('#')[1] : m[3].replace(/^https?:\/\/[^/]+/, ''))
        : m[2];
      tokens.push({ t: 'link', display: label, href: m[3] });
    } else if (m[4]) {
      // Backtick code
      tokens.push({ t: 'code', v: m[4].slice(1, -1) });
    } else if (m[5]) {
      // Bare section citation [Section 10.5.40.10] or [10.5.40.10]
      const id  = m[6];   // captured inside brackets
      const url = sectionIdToUrl(id);
      tokens.push({ t: 'secref', v: m[5], id, url });
    } else if (m[7]) {
      // Raw URL
      tokens.push({ t: 'rawurl', v: m[7] });
    } else if (m[8]) {
      // Bold
      tokens.push({ t: 'bold', v: m[8].slice(2, -2) });
    }

    cursor = RE.lastIndex;
  }

  if (cursor < text.length) tokens.push({ t: 'text', v: text.slice(cursor) });
  return tokens;
}

/** Render an inline-parsed token list */
function InlineText({ text }) {
  const tokens = parseInline(text);
  return (
    <>
      {tokens.map((tok, i) => {
        if (tok.t === 'bold') {
          return <strong key={i} className="md-bold">{tok.v}</strong>;
        }
        if (tok.t === 'code') {
          return <code key={i} className="md-code">{tok.v}</code>;
        }
        if (tok.t === 'secref') {
          // Section citation — styled as amber code; linked if URL can be built
          const inner = <code className="md-code">{tok.v}</code>;
          return tok.url
            ? <a key={i} href={tok.url} target="_blank" rel="noreferrer" className="md-secref-link">{inner}</a>
            : <span key={i}>{inner}</span>;
        }
        if (tok.t === 'link') {
          return (
            <a key={i} href={tok.href} target="_blank" rel="noreferrer" className="md-inline-link">
              {tok.display}
            </a>
          );
        }
        if (tok.t === 'rawurl') {
          // Shorten display: show just the path+fragment, not full domain
          const display = tok.v.replace(/^https?:\/\/[^/]+/, '') || tok.v;
          return (
            <a key={i} href={tok.v} target="_blank" rel="noreferrer" className="md-inline-link">
              {display}
            </a>
          );
        }
        return tok.v;
      })}
    </>
  );
}

/** Render a markdown table accumulated as rows of raw text */
function TableBlock({ rows }) {
  if (rows.length < 1) return null;
  const parseCells = (row) =>
    row.split('|').map(c => c.trim()).filter((c, i, a) => i > 0 && i < a.length - 1 || (i === 0 && c) || (i === a.length - 1 && c));

  const isSep = (row) => /^[\s|:\-]+$/.test(row);

  const headerRow = parseCells(rows[0]);
  const bodyRows  = rows.slice(1).filter(r => !isSep(r)).map(parseCells);

  return (
    <div className="md-table-wrap">
      <table className="md-table">
        <thead>
          <tr>{headerRow.map((h, i) => <th key={i} className="md-th"><InlineText text={h} /></th>)}</tr>
        </thead>
        <tbody>
          {bodyRows.map((cells, ri) => (
            <tr key={ri} className="md-tr">
              {cells.map((c, ci) => <td key={ci} className="md-td"><InlineText text={c} /></td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function MarkdownMessage({ content }) {
  if (!content) return null;

  const lines  = content.split('\n');
  const output = [];
  let bullets   = [];
  let numbered  = [];
  let tableRows = [];

  function flushBullets() {
    if (!bullets.length) return;
    output.push(
      <ul key={`ul-${output.length}`} className="md-ul">
        {bullets.map((item, i) => (
          <li key={i} className="md-li">
            <span className="md-li-content"><InlineText text={item} /></span>
          </li>
        ))}
      </ul>
    );
    bullets = [];
  }

  function flushNumbered() {
    if (!numbered.length) return;
    output.push(
      <ol key={`ol-${output.length}`} className="md-ol">
        {numbered.map((item, i) => (
          <li key={i} className="md-li">
            <span className="md-li-content"><InlineText text={item} /></span>
          </li>
        ))}
      </ol>
    );
    numbered = [];
  }

  function flushTable() {
    if (!tableRows.length) return;
    output.push(<TableBlock key={`tbl-${output.length}`} rows={tableRows} />);
    tableRows = [];
  }

  function flush() { flushBullets(); flushNumbered(); flushTable(); }

  lines.forEach((raw, idx) => {
    const trimmed = raw.trim();

    // Table row — starts and ends with |
    if (trimmed.startsWith('|')) {
      flushBullets(); flushNumbered();
      tableRows.push(trimmed);
      return;
    }

    // Non-table line — flush any in-progress table
    if (tableRows.length) flushTable();

    // Blank line
    if (!trimmed) {
      flush();
      output.push(<div key={`gap-${idx}`} className="md-gap" />);
      return;
    }

    // Horizontal rule
    if (/^[-*_]{3,}$/.test(trimmed)) {
      flush();
      output.push(<hr key={`hr-${idx}`} className="md-hr" />);
      return;
    }

    // Blockquote  >  (used for callout boxes)
    if (trimmed.startsWith('> ')) {
      flush();
      output.push(
        <div key={`bq-${idx}`} className="md-blockquote">
          <InlineText text={trimmed.slice(2)} />
        </div>
      );
      return;
    }

    // Bullet list  — * / • / -
    const bulletMatch = trimmed.match(/^[*•\-]\s+(.+)/);
    if (bulletMatch) {
      flushNumbered(); flushTable();
      bullets.push(bulletMatch[1]);
      return;
    }

    // Numbered list — 1. 2. etc
    const numMatch = trimmed.match(/^\d+\.\s+(.+)/);
    if (numMatch) {
      flushBullets(); flushTable();
      numbered.push(numMatch[1]);
      return;
    }

    flush();

    // ⚠️ warning / ✅ success / ❌ error status lines
    if (trimmed.startsWith('⚠')) {
      output.push(
        <div key={`warn-${idx}`} className="md-warn">
          <InlineText text={trimmed} />
        </div>
      );
      return;
    }
    if (trimmed.startsWith('✅')) {
      output.push(
        <div key={`ok-${idx}`} className="md-ok">
          <InlineText text={trimmed} />
        </div>
      );
      return;
    }
    if (trimmed.startsWith('❌')) {
      output.push(
        <div key={`err-${idx}`} className="md-err">
          <InlineText text={trimmed} />
        </div>
      );
      return;
    }

    // 📄 Source — with clickable links
    if (trimmed.startsWith('📄')) {
      output.push(<SourceLine key={`src-${idx}`} text={trimmed} />);
      return;
    }

    // ### heading
    const h3 = trimmed.match(/^###\s+(.+)/);
    if (h3) {
      output.push(
        <p key={`h3-${idx}`} className="md-h3"><InlineText text={h3[1]} /></p>
      );
      return;
    }

    // ## heading
    const h2 = trimmed.match(/^##\s+(.+)/);
    if (h2) {
      output.push(
        <p key={`h2-${idx}`} className="md-h2"><InlineText text={h2[1]} /></p>
      );
      return;
    }

    // Plain paragraph
    output.push(
      <p key={`p-${idx}`} className="md-p"><InlineText text={trimmed} /></p>
    );
  });

  flush();

  return <div className="md-root">{output}</div>;
}
