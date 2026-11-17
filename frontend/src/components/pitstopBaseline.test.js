import { describe, expect, it } from "vitest";
import { buildIssueGroups, groupPitStopIssues, orderIssueGroups } from "./issueGroups.js";
import { displayIssue, isHiddenIssue, replaceTrailingCount, visibleIssueGroups, visiblePitStopIssues } from "./pitstopBaseline.js";
import { clippingGroup, prepressNotices } from "./prepress.js";

const clipping = (extra = {}) => ({
  severity: "warning", action_id: 2079, occurrences: 1670,
  message: "Clipping objects found (1670x on pages 1-2)",
  locations: [{ page: 1, bbox: { min_x: 0, min_y: 0, max_x: 1, max_y: 1 } }],
  ...extra,
});
const hidden = clipping({ severity: "information", hidden_by_baseline: true, introduced_by_fix: 1670, baseline_occurrences: 0, original_severity: "warning" });
const partial = clipping({ baseline_occurrences: 168, introduced_by_fix: 1502 });
const other = { severity: "error", action_id: 1001, message: "Font not embedded", occurrences: 2 };

describe("replaceTrailingCount", () => {
  it("replaces only the trailing count and keeps the rest", () => {
    expect(replaceTrailingCount("Clipping objects found (1670x on pages 1-2)", 168)).toBe("Clipping objects found (168x on pages 1-2)");
    expect(replaceTrailingCount("Clipping objects found (1670x on page 3).", 5)).toBe("Clipping objects found (5x on page 3).");
  });
  it("leaves messages without a trailing count untouched", () => {
    expect(replaceTrailingCount("Font not embedded", 1)).toBe("Font not embedded");
    expect(replaceTrailingCount("Size 90x50 mm", 1)).toBe("Size 90x50 mm");
  });
});

describe("visiblePitStopIssues", () => {
  it("drops hidden issues and uses the client's count for partial ones", () => {
    const result = visiblePitStopIssues([hidden, partial, other]);
    expect(result).toHaveLength(2);
    expect(result[0].message).toBe("Clipping objects found (168x on pages 1-2)");
    expect(result[0].occurrences).toBe(168);
    expect(result[1]).toBe(other);
    expect(partial.message).toContain("1670x");
  });
  it("returns issues without baseline fields unchanged", () => {
    const issues = [clipping(), other];
    const result = visiblePitStopIssues(issues);
    expect(result).toEqual(issues);
    expect(result[0]).toBe(issues[0]);
    expect(displayIssue(other)).toBe(other);
    expect(isHiddenIssue(other)).toBe(false);
    expect(visiblePitStopIssues(undefined)).toEqual([]);
  });
});

describe("grouping ignores hidden issues", () => {
  it("groupPitStopIssues and buildIssueGroups skip hidden and show the client's count", () => {
    expect(groupPitStopIssues([hidden])).toEqual([]);
    const groups = groupPitStopIssues([hidden, partial, other]);
    expect(groups.map((group) => group.code)).toEqual(["action:1001", "action:2079"]);
    expect(groups[1].samples[0]).toBe("Clipping objects found (168x on pages 1-2)");
    expect(buildIssueGroups({ pitstop: { issues: [hidden] } })).toEqual([]);
  });

  const backendGroups = [
    { code: "action:2079", source: "pitstop", severity: "info", fix_plan: "remove_clipping", message: "Обтравка — 1 объект на странице 1", count: 1, samples: [hidden.message] },
    { code: "action:1001", source: "pitstop", severity: "error", message: "Font not embedded", count: 1, samples: ["Font not embedded"] },
    { code: "local", source: "local", severity: "error", message: "Локальная ошибка", count: 1 },
  ];
  it("removes backend pitstop groups that only hold hidden issues", () => {
    const pitstop = { issues: [hidden, other] };
    expect(visibleIssueGroups(backendGroups, pitstop).map((group) => group.code)).toEqual(["action:1001", "local"]);
    expect(orderIssueGroups({ issue_groups: backendGroups, pitstop }).map((group) => group.code)).toEqual(["action:1001", "local"]);
  });
  it("does not offer the clipping notice for a fix-introduced clipping issue", () => {
    expect(clippingGroup({ issue_groups: backendGroups, pitstop: { issues: [hidden, other] } })).toBeNull();
    expect(prepressNotices({ issue_groups: backendGroups, pitstop: { issues: [hidden] } }).map((n) => n.key)).not.toContain("clipping");
  });
  it("rewrites the raw count in backend group texts for partial issues", () => {
    const groups = [{ code: "action:2079", source: "pitstop", severity: "warning", message: partial.message, count: 1, samples: [partial.message] }];
    const [group] = visibleIssueGroups(groups, { issues: [partial] });
    expect(group.message).toBe("Clipping objects found (168x on pages 1-2)");
    expect(group.samples).toEqual(["Clipping objects found (168x on pages 1-2)"]);
  });
  it("returns groups as is when nothing was adjusted", () => {
    expect(visibleIssueGroups(backendGroups, { issues: [other] })).toBe(backendGroups);
    expect(visibleIssueGroups(backendGroups, undefined)).toBe(backendGroups);
  });
});
