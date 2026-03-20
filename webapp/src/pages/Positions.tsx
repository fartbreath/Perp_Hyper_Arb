/**
 * Positions -- Live open positions polled from /positions every 5 s.
 *
 * Maker positions: grouped by market into YES+NO spread rows showing
 *   combined est. close P&L at the live book (bid/ask), rebates earned,
 *   and a single Close button that liquidates both legs.
 *
 * Mispricing positions: individual rows (unchanged).
 * HL hedges: individual rows (unchanged).
 */
import { useState } from "react";
import { usePositions, useMakerSignals, undeployQuote, closePosition } from "../api/client";
import type { Position } from "../api/client";
import { usePolymarketEventSlugs } from "../utils/usePolymarketEventSlugs";

function timeSince(iso: string | null | undefined): string {
  if (!iso) return "\u2014";
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60_000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function timeUntilEnd(iso: string | null | undefined): { label: string; color: string } {
  if (!iso) return { label: "\u2014", color: "#4b5563" };
  const diff = new Date(iso).getTime() - Date.now();
  if (diff <= 0) return { label: "Resolved", color: "#6b7280" };
  const totalMin = Math.floor(diff / 60_000);
  const h = Math.floor(totalMin / 60);
  const d = Math.floor(h / 24);
  if (d >= 2) return { label: `${d}d ${h % 24}h`, color: "#94a3b8" };
  if (h >= 4) return { label: `${h}h ${totalMin % 60}m`, color: "#f59e0b" };
  if (h >= 1) return { label: `${h}h ${totalMin % 60}m`, color: "#ef4444" };
  return { label: `${totalMin}m`, color: "#ef4444" };
}

const sideBg = (side: string) => {
  if (side === "YES" || side === "LONG") return "#166534";
  if (side === "NO" || side === "SHORT") return "#7f1d1d";
  return "#374151";
};

function pnlColor(v: number | null | undefined): string {
  if (v == null) return "#94a3b8";
  return v >= 0 ? "#22c55e" : "#ef4444";
}
function pnlStr(v: number | null | undefined): string {
  if (v == null) return "\u2014";
  return `${v >= 0 ? "+" : ""}$${v.toFixed(2)}`;
}
function scoreColor(s: number | null | undefined): string {
  if (s == null) return "#4b5563";
  if (s >= 80) return "#22c55e";
  if (s >= 60) return "#86efac";
  if (s >= 40) return "#fbbf24";
  return "#ef4444";
}

// -- Maker spread row ----------------------------------------------------------

interface SpreadRowProps {
  marketId: string;
  yes: Position | undefined;
  no: Position | undefined;
  closeState: string | undefined;
  onClose: (id: string) => void;
  marketUrl: string | null;
}

function SpreadRow({ marketId, yes, no, closeState, onClose, marketUrl }: SpreadRowProps) {
  const rep = yes ?? no!;  // at least one is defined

  // Entry prices in token-space (YES token and NO token cents)
  const entryYesCents = yes ? yes.entry_price * 100 : null;
  const entryNoCents  = no  ? (1 - no.entry_price) * 100 : null;

  // Spread captured at entry (cents): how wide the bid/ask we straddled
  const entryCaptured = (entryYesCents != null && entryNoCents != null)
    ? (100 - entryYesCents - entryNoCents) : null;

  // Current book prices
  const bid = yes?.yes_book_bid ?? no?.yes_book_bid;
  const ask = yes?.yes_book_ask ?? no?.yes_book_ask;
  const bookAge = yes?.book_age_s ?? no?.book_age_s;

  // Combined deployed capital
  const deployed = (yes?.entry_cost_usd ?? 0) + (no?.entry_cost_usd ?? 0);

  // Combined rebates earned so far
  const rebates = (yes?.pm_rebates_earned ?? 0) + (no?.pm_rebates_earned ?? 0);

  // Estimated close P&L: sum of each leg's server-computed est_close_pnl
  // (already includes entry rebates + exit rebate - exit fee for each leg)
  const estYes = yes?.est_close_pnl ?? null;
  const estNo  = no?.est_close_pnl  ?? null;
  const estTotal = (estYes != null || estNo != null)
    ? ((estYes ?? 0) + (estNo ?? 0)) : null;

  // Avg score
  const scores = [yes?.signal_score, no?.signal_score].filter((s) => s != null) as number[];
  const avgScore = scores.length ? scores.reduce((a, b) => a + b, 0) / scores.length : null;

  const isComplete = yes != null && no != null;
  const contracts = yes?.contracts ?? no?.contracts ?? 0;

  // Matched (hedged) contract pairs — the smaller of the two legs.
  // When legs are unequal, the excess is a naked directional position.
  const yesContracts = yes?.contracts ?? 0;
  const noContracts  = no?.contracts  ?? 0;
  const matchedContracts = isComplete ? Math.min(yesContracts, noContracts) : contracts;
  const nakedContracts   = isComplete ? Math.abs(yesContracts - noContracts) : 0;
  const nakedSide        = yesContracts > noContracts ? "YES" : "NO";

  // Spread @ Expiry: outcome-neutral P&L for the MATCHED pairs only.
  // Naked contracts are directional and are NOT included — their P&L depends
  // on market resolution direction and would make this metric misleading.
  const spreadAtExpiry = (isComplete && entryCaptured != null)
    ? matchedContracts * (entryCaptured / 100) + rebates
    : null;
  const earliest = [yes?.opened_at, no?.opened_at]
    .filter(Boolean)
    .sort()[0];

  const isBusy = closeState === "closing";
  const hasDone = !!closeState && closeState !== "closing";

  return (
    <tr>
      {/* Market */}
      <td title={rep.market_title} style={{ maxWidth: 260, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {marketUrl ? (
          <a href={marketUrl} target="_blank" rel="noopener noreferrer">{rep.market_title}</a>
        ) : rep.market_title}
      </td>

      {/* Underlying */}
      <td><strong>{rep.underlying}</strong></td>

      {/* Spread status */}
      <td>
        {isComplete ? (
          <span style={{ padding: "2px 7px", borderRadius: 4, fontSize: "0.75rem", fontWeight: 600, background: "#1e3a5f", color: "#60a5fa" }}>
            SPREAD
          </span>
        ) : (
          <span title="Only one side filled so far"
            style={{ padding: "2px 7px", borderRadius: 4, fontSize: "0.75rem", fontWeight: 600, background: "#292524", color: "#f97316" }}>
            {yes ? "YES only" : "NO only"}
          </span>
        )}
      </td>

      {/* Score */}
      <td style={{ fontFamily: "monospace", fontWeight: 600, color: scoreColor(avgScore) }}
          title={`Score at fill — YES: ${yes?.signal_score ?? "\u2014"}, NO: ${no?.signal_score ?? "\u2014"}`}>
        {avgScore != null ? avgScore.toFixed(0) : "\u2014"}
      </td>

      {/* Entry spread */}
      <td style={{ fontFamily: "monospace", fontSize: "0.8rem" }}
          title={
            entryYesCents != null && entryNoCents != null
              ? `Bought YES @ ${entryYesCents.toFixed(1)}\u00A2  |  Sold NO @ ${entryNoCents.toFixed(1)}\u00A2 token price\nSpread captured: ${entryCaptured?.toFixed(1)}\u00A2`
              : "Partial fill — one side pending"
          }>
        {entryYesCents != null ? (
          <span style={{ color: "#22c55e" }}>{entryYesCents.toFixed(1)}&cent;</span>
        ) : <span style={{ color: "#4b5563" }}>&mdash;</span>}
        {" / "}
        {entryNoCents != null ? (
          <span style={{ color: "#ef4444" }}>{entryNoCents.toFixed(1)}&cent;</span>
        ) : <span style={{ color: "#4b5563" }}>&mdash;</span>}
        {entryCaptured != null && (
          <span style={{ color: "#64748b", marginLeft: 4 }}>({entryCaptured.toFixed(1)}&cent;)</span>
        )}
      </td>

      {/* Contracts x Deployed */}
      <td style={{ fontFamily: "monospace" }}
          title={
            isComplete
              ? (
                  `YES: ${yesContracts.toFixed(0)} ct  |  NO: ${noContracts.toFixed(0)} ct\n` +
                  (nakedContracts > 0
                    ? `⚠ ${nakedContracts.toFixed(0)} naked ${nakedSide} contracts (unhedged directional exposure)\n`
                    : `Fully matched spread\n`) +
                  `$${deployed.toFixed(2)} capital deployed`
                )
              : `${contracts.toFixed(0)} contracts (${yes ? "YES" : "NO"} only)\n$${deployed.toFixed(2)} capital deployed`
          }>
        <span style={{ color: "#94a3b8", fontSize: "0.8rem" }}>{contracts.toFixed(0)}ct</span>
        {nakedContracts > 0 && (
          <span style={{ color: "#f97316", fontSize: "0.72rem", marginLeft: 3 }}
                title={`${nakedContracts.toFixed(0)} naked ${nakedSide} contracts — unhedged directional exposure`}>
            ⚠{nakedContracts.toFixed(0)}
          </span>
        )}
        {" "}
        <span style={{ color: "#f59e0b", fontWeight: 600 }}>${deployed.toFixed(2)}</span>
      </td>

      {/* YES book bid/ask */}
      <td style={{ fontFamily: "monospace", fontSize: "0.8rem" }}
          title={bid != null && ask != null ? `YES bid: ${(bid * 100).toFixed(1)}\u00A2  |  YES ask: ${(ask * 100).toFixed(1)}\u00A2` : "Book unavailable"}>
        {bid != null ? (
          <span style={{ color: "#22c55e" }}>{(bid * 100).toFixed(1)}</span>
        ) : <span style={{ color: "#4b5563" }}>&mdash;</span>}
        {" / "}
        {ask != null ? (
          <span style={{ color: "#ef4444" }}>{(ask * 100).toFixed(1)}</span>
        ) : <span style={{ color: "#4b5563" }}>&mdash;</span>}
        {bookAge != null && bookAge > 120 && (
          <span style={{ color: "#ef4444", marginLeft: 4, fontSize: "0.72rem" }} title={`Book is ${bookAge}s old`}>⚠</span>
        )}
        {bookAge != null && bookAge > 30 && bookAge <= 120 && (
          <span style={{ color: "#f97316", marginLeft: 4, fontSize: "0.72rem" }} title={`Book is ${bookAge}s old`}>⚠</span>
        )}
      </td>

      {/* NO book bid/ask: NO bid = 1 - YES ask, NO ask = 1 - YES bid */}
      <td style={{ fontFamily: "monospace", fontSize: "0.8rem" }}
          title={bid != null && ask != null ? `NO bid: ${((1 - ask) * 100).toFixed(1)}\u00A2  |  NO ask: ${((1 - bid) * 100).toFixed(1)}\u00A2` : "Book unavailable"}>
        {ask != null ? (
          <span style={{ color: "#22c55e" }}>{((1 - ask) * 100).toFixed(1)}</span>
        ) : <span style={{ color: "#4b5563" }}>&mdash;</span>}
        {" / "}
        {bid != null ? (
          <span style={{ color: "#ef4444" }}>{((1 - bid) * 100).toFixed(1)}</span>
        ) : <span style={{ color: "#4b5563" }}>&mdash;</span>}
      </td>

      {/* Rebates earned */}
      <td style={{ fontFamily: "monospace", color: "#a78bfa" }}
          title="Maker rebates credited from entry fills. Exit rebate is already included in Est. P&L.">
        {rebates > 0 ? `+$${rebates.toFixed(3)}` : "\u2014"}
      </td>

      {/* Est close P&L */}
      <td style={{ fontFamily: "monospace", fontWeight: 700, color: pnlColor(estTotal), fontSize: "0.9rem" }}
          title={
            `Estimated P&L if both legs closed NOW at best bid/ask.\n` +
            `YES leg: ${pnlStr(estYes)}  |  NO leg: ${pnlStr(estNo)}\n` +
            `Includes entry rebates + projected exit rebate − taker exit fees`
          }>
        {pnlStr(estTotal)}
      </td>
      {/* Spread @ Expiry */}
      <td style={{ fontFamily: "monospace", fontWeight: 700, color: spreadAtExpiry != null ? "#22c55e" : "#4b5563", fontSize: "0.9rem" }}
          title={
            spreadAtExpiry != null
              ? (
                  `Outcome-neutral P&L for the ${matchedContracts.toFixed(0)} matched pairs held to resolution.\n` +
                  `${matchedContracts.toFixed(0)}ct \u00D7 ${entryCaptured?.toFixed(1)}\u00A2 spread + $${rebates.toFixed(3)} rebates.` +
                  (nakedContracts > 0
                    ? `\n\n⚠ ${nakedContracts.toFixed(0)} naked ${nakedSide} contracts NOT included — their P&L depends on resolution direction.`
                    : `\nCompare to Est. Close P&L to decide: close now, or hold to expiry.`)
                )
              : "Only available for complete spreads (both YES and NO filled)"
          }>
        {spreadAtExpiry != null ? `+$${spreadAtExpiry.toFixed(3)}` : "\u2014"}
        {spreadAtExpiry != null && nakedContracts > 0 && (
          <span style={{ color: "#f97316", fontSize: "0.7rem", marginLeft: 2 }}>⚠</span>
        )}
      </td>
      {/* Opened */}
      <td className="muted">{timeSince(earliest)}</td>

      {/* Ends */}
      <td style={{ fontFamily: "monospace", fontWeight: 600, color: timeUntilEnd(rep.end_date).color }}
          title={rep.end_date ? new Date(rep.end_date).toLocaleString() : undefined}>
        {timeUntilEnd(rep.end_date).label}
      </td>

      {/* Action */}
      <td>
        {hasDone ? (
          <span style={{ fontSize: "0.75rem", color: "#94a3b8", display: "block", maxWidth: 120 }}>
            {closeState}
          </span>
        ) : (
          <button
            disabled={isBusy}
            onClick={() => onClose(marketId)}
            title={isComplete ? "Close both YES and NO legs at best bid/ask" : "Close open leg at best bid/ask"}
            style={{
              padding: "3px 10px", fontSize: "0.78rem", borderRadius: 4,
              border: "1px solid #ef4444", background: "transparent",
              color: "#ef4444", cursor: isBusy ? "wait" : "pointer",
              opacity: isBusy ? 0.5 : 1,
            }}
          >
            {isBusy ? "Closing..." : isComplete ? "Close Spread" : "Close"}
          </button>
        )}
      </td>
    </tr>
  );
}

// -- Main component ------------------------------------------------------------

export default function Positions() {
  const { data, loading, error } = usePositions();
  const slugMap = usePolymarketEventSlugs();
  const [closeState, setCloseState] = useState<Record<string, string>>({});
  const { data: signalsData } = useMakerSignals();
  const [orderPending, setOrderPending] = useState<string | null>(null);
  const openOrders = (signalsData?.signals ?? []).filter((s) => s.is_deployed);

  const handleClose = async (marketId: string) => {
    setCloseState((s) => ({ ...s, [marketId]: "closing" }));
    try {
      const result = await closePosition(marketId);
      const sidesStr = (result.sides_closed ?? []).join("+") || "?";
      setCloseState((s) => ({
        ...s,
        [marketId]: `${sidesStr} closed \u00B7 P&L ${result.pnl >= 0 ? "+" : ""}$${result.pnl.toFixed(2)}`,
      }));
    } catch (e: unknown) {
      setCloseState((s) => ({
        ...s,
        [marketId]: e instanceof Error ? e.message : "Error",
      }));
    }
  };

  const handleUndeploy = async (tokenId: string) => {
    setOrderPending(tokenId);
    try { await undeployQuote(tokenId); } catch { /* handled by server */ }
    finally { setOrderPending(null); }
  };

  const all = data?.positions ?? [];
  const pmPositions = all.filter((p) => p.venue === "PM");
  const hlHedges = all.filter((p) => p.venue === "HL");

  // Split PM positions into maker and mispricing
  const makerPositions = pmPositions.filter((p) => p.strategy === "maker");
  const mispricingPositions = pmPositions.filter((p) => p.strategy !== "maker");

  // Group maker positions by condition_id -> Map<condition_id, {yes?, no?}>
  const makerSpreads = new Map<string, { yes?: Position; no?: Position }>();
  for (const pos of makerPositions) {
    const existing = makerSpreads.get(pos.condition_id) ?? {};
    if (pos.side === "YES") existing.yes = pos;
    else existing.no = pos;
    makerSpreads.set(pos.condition_id, existing);
  }

  return (
    <div className="page">
      <h2>
        Open Positions
        {data && (
          <span className="muted" style={{ fontSize: "0.85rem", marginLeft: "0.75rem" }}>
            {makerSpreads.size} maker spread{makerSpreads.size !== 1 ? "s" : ""}
            {mispricingPositions.length > 0 && ` \u00B7 ${mispricingPositions.length} mispricing`}
            {" · "}{hlHedges.length} HL hedge{hlHedges.length !== 1 ? "s" : ""}
          </span>
        )}
      </h2>

      {error && <div className="error">Failed to load positions: {error}</div>}
      {loading && !data && <div className="skeleton" style={{ height: 200 }} />}

      {/* -- Maker spreads ------------------------------------- */}
      <h3 style={{ marginTop: "1.5rem", marginBottom: "0.5rem", fontSize: "0.9rem", color: "#9ca3af" }}>
        Market Making Spreads
      </h3>
      {!loading && makerSpreads.size === 0 && (
        <div className="muted" style={{ paddingBottom: "1rem" }}>No open maker positions.</div>
      )}
      {makerSpreads.size > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table className="data-table">
            <thead>
              <tr>
                <th>Market</th>
                <th>Underlying</th>
                <th>Status</th>
                <th title="Signal quality score 0–100 (avg of both legs)" style={{ cursor: "help", borderBottom: "1px dashed #6b7280" }}>Score</th>
                <th title="YES entry / NO entry (as YES-token prices). Value in () is spread captured." style={{ cursor: "help", borderBottom: "1px dashed #6b7280" }}>Entry YES / NO</th>
                <th title="Contract count × actual USD capital deployed" style={{ cursor: "help", borderBottom: "1px dashed #6b7280" }}>Size × Deployed</th>
                <th title="Live YES-token bid / ask" style={{ cursor: "help", borderBottom: "1px dashed #6b7280" }}>YES Bid/Ask</th>
                <th title="NO-token bid / ask (= 1 - YES ask/bid)" style={{ cursor: "help", borderBottom: "1px dashed #6b7280" }}>NO Bid/Ask</th>
                <th title="Maker rebates credited from fills so far" style={{ cursor: "help", borderBottom: "1px dashed #6b7280" }}>Rebates</th>
                <th title="Estimated combined P&L if both legs closed NOW at bid/ask. Includes entry rebates + projected exit rebate − taker fees." style={{ cursor: "help", borderBottom: "1px dashed #6b7280" }}>Est. Close P&L</th>                <th title="Guaranteed P&L if both legs held to expiry (outcome-neutral). Only shown for complete spreads. Compare to Est. Close P&L." style={{ cursor: "help", borderBottom: "1px dashed #6b7280" }}>Spread @ Expiry</th>                <th>Opened</th>
                <th>Ends</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              {Array.from(makerSpreads.entries()).map(([marketId, { yes, no }]) => {
                const rep = yes ?? no!;
                const slug = rep.market_slug || slugMap[marketId];
                const marketUrl = slug ? `https://polymarket.com/event/${slug}` : null;
                return (
                  <SpreadRow
                    key={marketId}
                    marketId={marketId}
                    yes={yes}
                    no={no}
                    closeState={closeState[marketId]}
                    onClose={handleClose}
                    marketUrl={marketUrl}
                  />
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* -- Mispricing positions ------------------------------ */}
      {mispricingPositions.length > 0 && (
        <>
          <h3 style={{ marginTop: "2rem", marginBottom: "0.5rem", fontSize: "0.9rem", color: "#9ca3af" }}>
            Mispricing Positions
          </h3>
          <table className="data-table">
            <thead>
              <tr>
                <th>Market</th>
                <th>Underlying</th>
                <th>Side</th>
                <th title="Signal quality score" style={{ cursor: "help", borderBottom: "1px dashed #6b7280" }}>Score</th>
                <th title="Actual USDC capital deployed at fill" style={{ cursor: "help", borderBottom: "1px dashed #6b7280" }}>USD Deployed</th>
                <th>Entry price</th>
                <th>Current price</th>
                <th title="Unrealised P&L computed server-side">Unrealised P&L</th>
                <th title="Progress toward profit target" style={{ cursor: "help", borderBottom: "1px dashed #6b7280" }}>% of Max Gain</th>
                <th>Opened</th>
                <th>Ends</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              {mispricingPositions.map((pos) => {
                const marketId = pos.condition_id;
                const slug = pos.market_slug || slugMap[marketId];
                const marketUrl = slug ? `https://polymarket.com/event/${slug}` : null;
                const tokenPrice = pos.token_entry_price ?? (pos.side === "NO" ? 1 - pos.entry_price : pos.entry_price);
                const currentTokenPrice = pos.token_current_price ?? (pos.current_mid == null ? null : pos.side === "NO" ? 1 - pos.current_mid : pos.current_mid);
                const unrealizedPnl = pos.unrealised_pnl_usd ?? (currentTokenPrice != null ? (currentTokenPrice - tokenPrice) * pos.contracts : null);
                const contracts = pos.contracts ?? pos.size_usd;
                return (
                  <tr key={marketId + pos.side}>
                    <td title={pos.market_title} style={{ maxWidth: 300, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {marketUrl ? <a href={marketUrl} target="_blank" rel="noopener noreferrer">{pos.market_title}</a> : pos.market_title}
                    </td>
                    <td>{pos.underlying}</td>
                    <td>
                      <span style={{ padding: "2px 8px", borderRadius: 4, fontSize: "0.8rem", fontWeight: 600, background: sideBg(pos.side), color: "#fff" }}>
                        {pos.side}
                      </span>
                    </td>
                    <td style={{ fontFamily: "monospace", fontWeight: 600, color: scoreColor(pos.signal_score) }}
                        title={`Signal score: ${pos.signal_score ?? "n/a"}`}>
                      {pos.signal_score != null ? pos.signal_score.toFixed(0) : "\u2014"}
                    </td>
                    <td style={{ fontFamily: "monospace", color: "#f59e0b", fontWeight: 600 }}
                        title={`At price ${(tokenPrice * 100).toFixed(1)}\u00A2 \u00D7 ${contracts.toFixed(0)} contracts`}>
                      ${(pos.entry_cost_usd ?? 0).toFixed(2)}
                    </td>
                    <td style={{ fontFamily: "monospace" }}>{(tokenPrice * 100).toFixed(2)}&cent;</td>
                    <td style={{ fontFamily: "monospace", color: "#94a3b8" }}>
                      {currentTokenPrice != null ? `${(currentTokenPrice * 100).toFixed(1)}\u00A2` : "\u2014"}
                      {pos.book_age_s != null && pos.book_age_s > 120 && (
                        <span style={{ color: "#ef4444", marginLeft: 4, fontSize: "0.75rem" }} title={`Book is ${pos.book_age_s}s old`}>⚠</span>
                      )}
                    </td>
                    <td style={{ fontFamily: "monospace", fontWeight: 600, color: pnlColor(unrealizedPnl) }}>
                      {pnlStr(unrealizedPnl)}
                    </td>
                    <td style={{ fontFamily: "monospace", fontWeight: 600,
                        color: pos.pct_of_target == null ? "#4b5563" : pos.pct_of_target >= 100 ? "#22c55e" : pos.pct_of_target >= 50 ? "#86efac" : pos.pct_of_target >= 0 ? "#fbbf24" : "#ef4444" }}
                        title={pos.profit_target_usd != null ? `Target: $${pos.profit_target_usd.toFixed(2)}` : "Awaiting data"}>
                      {pos.pct_of_target == null ? "\u2014" : `${pos.pct_of_target.toFixed(1)}%`}
                    </td>
                    <td className="muted">{timeSince(pos.opened_at)}</td>
                    <td style={{ fontFamily: "monospace", fontWeight: 600, color: timeUntilEnd(pos.end_date).color }}
                        title={pos.end_date ? new Date(pos.end_date).toLocaleString() : undefined}>
                      {timeUntilEnd(pos.end_date).label}
                    </td>
                    <td>
                      {closeState[marketId] && closeState[marketId] !== "closing" ? (
                        <span style={{ fontSize: "0.78rem", color: "#94a3b8" }}>{closeState[marketId]}</span>
                      ) : (
                        <button
                          disabled={closeState[marketId] === "closing"}
                          onClick={() => handleClose(marketId)}
                          style={{ padding: "3px 10px", fontSize: "0.78rem", borderRadius: 4, border: "1px solid #ef4444", background: "transparent", color: "#ef4444", cursor: closeState[marketId] === "closing" ? "wait" : "pointer", opacity: closeState[marketId] === "closing" ? 0.5 : 1 }}
                        >
                          {closeState[marketId] === "closing" ? "Closing..." : "Close"}
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </>
      )}

      {/* -- HL delta hedges --------------------------------- */}
      <h3 style={{ marginTop: "2rem", marginBottom: "0.5rem", fontSize: "0.9rem", color: "#9ca3af" }}>
        HyperLiquid Delta Hedges
      </h3>
      {!loading && hlHedges.length === 0 && (
        <div className="muted" style={{ paddingBottom: "1rem" }}>No active HL hedges (inventory below threshold).</div>
      )}
      {hlHedges.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Coin</th>
              <th>Direction</th>
              <th>Size (coins)</th>
              <th>Notional (USD)</th>
              <th>Entry Price</th>
            </tr>
          </thead>
          <tbody>
            {hlHedges.map((pos) => (
              <tr key={pos.condition_id}>
                <td><strong>{pos.underlying}</strong></td>
                <td>
                  <span style={{ padding: "2px 8px", borderRadius: 4, fontSize: "0.8rem", fontWeight: 600, background: sideBg(pos.side), color: "#fff" }}>
                    {pos.side}
                  </span>
                </td>
                <td>{pos.hl_size_coins != null ? pos.hl_size_coins.toFixed(4) : "\u2014"}</td>
                <td>${pos.size_usd.toFixed(2)}</td>
                <td>${pos.entry_price.toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {/* -- Open Orders (resting maker quotes) ----------------------------- */}
      <h3 style={{ marginTop: "2rem", marginBottom: "0.5rem", fontSize: "0.9rem", color: "#9ca3af" }}>
        Open Orders
        <span style={{ marginLeft: "0.5rem", fontSize: "0.8rem", color: "#475569", fontWeight: 400 }}>
          &mdash; resting CLOB quotes
        </span>
      </h3>
      {openOrders.length === 0 && (
        <div className="muted" style={{ paddingBottom: "1rem" }}>No open maker orders.</div>
      )}
      {openOrders.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table className="data-table" style={{ fontSize: "0.82rem" }}>
            <thead>
              <tr>
                <th>Market</th>
                <th>Underlying</th>
                <th>Mid</th>
                <th>Bid / Ask</th>
                <th>Spread</th>
                <th>Capital</th>
                <th>Bid Fill</th>
                <th>Ask Fill</th>
                <th>Type</th>
                <th title="Signal quality score 0–100" style={{ cursor: "help", borderBottom: "1px dashed #6b7280" }}>Score</th>
                <th>Age</th>
                <th>Ends</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {openOrders.map((s, i) => {
                const marketUrl = s.market_slug
                  ? `https://polymarket.com/event/${s.market_slug}`
                  : slugMap[s.market_id]
                  ? `https://polymarket.com/event/${slugMap[s.market_id]}`
                  : null;
                return (
                  <tr key={i} style={{ borderBottom: "1px solid #0f172a", background: i % 2 === 0 ? "transparent" : "#0f172a44" }}>
                    <td style={{ fontWeight: 600, maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {marketUrl ? (
                        <a href={marketUrl} target="_blank" rel="noopener noreferrer">{s.market_title}</a>
                      ) : s.market_title}
                    </td>
                    <td style={{ fontWeight: 600 }}>{s.underlying}</td>
                    <td style={{ fontFamily: "monospace" }}>{(s.mid * 100).toFixed(1)}&cent;</td>
                    <td style={{ fontFamily: "monospace" }}>
                      <span style={{ color: "#22c55e" }}>{(s.bid_price * 100).toFixed(1)}</span>
                      {" / "}
                      <span style={{ color: "#ef4444" }}>{(s.ask_price * 100).toFixed(1)}</span>
                    </td>
                    <td style={{ fontFamily: "monospace" }}>{(s.half_spread * 200).toFixed(1)}&cent;</td>
                    <td style={{ fontFamily: "monospace", color: "#f59e0b", fontWeight: 600 }}>
                      ${(s.collateral_usd ?? 0).toFixed(2)}
                    </td>
                    <td style={{ fontFamily: "monospace", fontSize: "0.78rem" }}>
                      {(s.bid_original_size ?? 0) > 0 ? (
                        <span title={`Bid: ${(s.bid_filled_size ?? 0).toFixed(0)} filled / ${(s.bid_original_size ?? 0).toFixed(0)}`}>
                          <span style={{ color: "#22c55e" }}>{(s.bid_filled_size ?? 0).toFixed(0)}</span>
                          <span style={{ color: "#475569" }}>/{(s.bid_original_size ?? 0).toFixed(0)}</span>
                          {(s.bid_remaining_size ?? 0) > 0 && (
                            <span style={{ color: "#64748b" }}> ({(s.bid_remaining_size ?? 0).toFixed(0)}↓)</span>
                          )}
                        </span>
                      ) : <span className="muted">&mdash;</span>}
                    </td>
                    <td style={{ fontFamily: "monospace", fontSize: "0.78rem" }}>
                      {(s.ask_original_size ?? 0) > 0 ? (
                        <span title={`Ask: ${(s.ask_filled_size ?? 0).toFixed(0)} filled / ${(s.ask_original_size ?? 0).toFixed(0)}`}>
                          <span style={{ color: "#ef4444" }}>{(s.ask_filled_size ?? 0).toFixed(0)}</span>
                          <span style={{ color: "#475569" }}>/{(s.ask_original_size ?? 0).toFixed(0)}</span>
                          {(s.ask_remaining_size ?? 0) > 0 && (
                            <span style={{ color: "#64748b" }}> ({(s.ask_remaining_size ?? 0).toFixed(0)}↓)</span>
                          )}
                        </span>
                      ) : <span className="muted">&mdash;</span>}
                    </td>
                    <td style={{ color: "#94a3b8", fontSize: "0.75rem" }}>{s.market_type}</td>
                    <td style={{ fontFamily: "monospace", fontWeight: 600, fontSize: "0.82rem", color: scoreColor(s.score) }}
                        title={`Signal score: ${s.score ?? "n/a"}`}>
                      {s.score != null ? s.score.toFixed(0) : "\u2014"}
                    </td>
                    <td style={{ color: "#64748b" }}>{s.age_seconds.toFixed(0)}s</td>
                    <td style={{ fontFamily: "monospace", fontWeight: 600, fontSize: "0.78rem", color: timeUntilEnd(s.end_date).color }}
                        title={s.end_date ? new Date(s.end_date).toLocaleString() : undefined}>
                      {timeUntilEnd(s.end_date).label}
                    </td>
                    <td>
                      <button
                        onClick={() => handleUndeploy(s.token_id)}
                        disabled={orderPending === s.token_id}
                        style={{ padding: "0.2rem 0.6rem", fontSize: "0.75rem", background: "#ef444422", color: "#ef4444", border: "1px solid #ef444444", borderRadius: 4, cursor: "pointer" }}
                      >
                        {orderPending === s.token_id ? "..." : "Cancel"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
