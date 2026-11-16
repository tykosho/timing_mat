// Live leaderboard updates (issue #71). Lap, start and resume messages carry only the runner's
// updated row (msg.row); this merges it into the page's copy of the leaderboard and keeps the
// server's order: most laps first, then whoever got there first, runners without laps last.
// The full board (leaderboard_update) still arrives every few seconds and replaces the copy.
function compareLeaderboardRows(a, b) {
  const laps = (b.total_laps || 0) - (a.total_laps || 0);
  if (laps !== 0) return laps;
  const ta = a.last_crossing_time_laps, tb = b.last_crossing_time_laps;
  if (ta == null && tb == null) return 0;
  if (ta == null) return 1;
  if (tb == null) return -1;
  return ta - tb;
}

function mergeLeaderboardRow(board, row) {
  const list = Array.isArray(board) ? board.slice() : [];
  if (!row || row.bib_number == null) return list;
  const i = list.findIndex(r => r.bib_number === row.bib_number);
  if (i === -1) list.push(row); else list[i] = row;
  return list.sort(compareLeaderboardRows);  // Array.prototype.sort is stable: ties keep their order
}

// The board a live message implies: its full leaderboard if it has one, else `board` + its row
function boardFromMessage(board, msg) {
  if (Array.isArray(msg.leaderboard)) return msg.leaderboard;
  if (msg.row) return mergeLeaderboardRow(board, msg.row);
  return board;
}
