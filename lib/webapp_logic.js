"use strict";
// Pure functions — no DOM, no Telegram dependency. Safe to require() in Node.js for testing.

function roundPrice(px) {
  if (px <= 0) return px;
  var mag = Math.floor(Math.log10(Math.abs(px)));
  var factor = Math.pow(10, 4 - mag);
  return Math.round(px * factor) / factor;
}

function calcLadderPrices(fromPx, toPx, nParts) {
  if (typeof fromPx !== "number" || typeof toPx !== "number")
    throw new Error("Prices must be numbers");
  if (nParts < 2 || nParts > 20) throw new Error("Parts must be 2–20");
  var prices = [];
  for (var i = 0; i < nParts; i++)
    prices.push(roundPrice(fromPx + (toPx - fromPx) * i / (nParts - 1)));
  return prices;
}

function calcNotional(price, size) { return price * size; }

function calcMargin(price, size, leverage) {
  if (leverage <= 0) throw new Error("Leverage must be positive");
  return (price * size) / leverage;
}

function fmtUSD(amount) {
  if (amount >= 1e6) return "$" + (amount / 1e6).toFixed(2) + "M";
  if (amount >= 1e3) return "$" + (amount / 1e3).toFixed(1) + "k";
  return "$" + amount.toFixed(2);
}

function fmtPrice(px) {
  if (px >= 10000) return "$" + Math.round(px).toLocaleString("en-US");
  if (px >= 1000)  return "$" + px.toLocaleString("en-US", {maximumFractionDigits: 1});
  if (px >= 1)     return "$" + px.toFixed(2);
  return "$" + px.toFixed(4);
}

function validateOpenForm(f) {
  var errors = [];
  if (!f.coin || !f.coin.trim()) errors.push("Coin is required");
  var sz = parseFloat(f.size);
  if (isNaN(sz) || sz <= 0) errors.push("Size must be greater than 0");
  var lv = parseInt(f.leverage);
  if (isNaN(lv) || lv < 1 || lv > 100) errors.push("Leverage must be 1–100");
  if (f.tp && f.sl) {
    var tp = parseFloat(f.tp), sl = parseFloat(f.sl);
    if (!isNaN(tp) && !isNaN(sl) && tp > 0 && sl > 0) {
      if (f.side === "long"  && tp <= sl) errors.push("TP must be above SL for longs");
      if (f.side === "short" && tp >= sl) errors.push("TP must be below SL for shorts");
    }
  }
  return errors;
}

function validateLadderOpenForm(f) {
  var errors = validateOpenForm(f);
  var fp = parseFloat(f.from_price), tp = parseFloat(f.to_price);
  if (isNaN(fp) || fp <= 0) errors.push("From price is required");
  if (isNaN(tp) || tp <= 0) errors.push("To price is required");
  if (!isNaN(fp) && !isNaN(tp) && fp === tp) errors.push("From and To prices must differ");
  var n = parseInt(f.parts);
  if (isNaN(n) || n < 2 || n > 20) errors.push("Parts must be 2–20");
  return errors;
}

function validateLadderCloseForm(f) {
  var errors = [];
  if (!f.coin || !f.coin.trim()) errors.push("Coin is required");
  var fp = parseFloat(f.from_price), tp = parseFloat(f.to_price);
  if (isNaN(fp) || fp <= 0) errors.push("From price is required");
  if (isNaN(tp) || tp <= 0) errors.push("To price is required");
  if (!isNaN(fp) && !isNaN(tp) && fp === tp) errors.push("From and To prices must differ");
  var n = parseInt(f.parts);
  if (isNaN(n) || n < 2 || n > 20) errors.push("Parts must be 2–20");
  return errors;
}

function buildOpenSummary(coin, side, size, leverage, price, mode, parts) {
  var dir = side === "long" ? "🟢 Long" : "🔴 Short";
  var lines = [dir + " " + size + " " + coin + " @ " + leverage + "× leverage"];
  if (price > 0) {
    lines.push("Notional: " + fmtUSD(price * parseFloat(size)));
    lines.push("Margin:   " + fmtUSD(price * parseFloat(size) / leverage));
  }
  if (mode === "ladder" && parts > 1) lines.push(parts + " limit orders");
  return lines.join("\n");
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    roundPrice, calcLadderPrices, calcNotional, calcMargin,
    fmtUSD, fmtPrice, validateOpenForm, validateLadderOpenForm,
    validateLadderCloseForm, buildOpenSummary
  };
}
