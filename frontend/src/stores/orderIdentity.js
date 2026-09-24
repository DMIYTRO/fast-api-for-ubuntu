/** Stable key for an order card within a run. */
export function orderIdentity(order) {
  if (!order) return "";
  if (order.aggregate_id != null && order.aggregate_id !== "") return `aggregate:${String(order.aggregate_id)}`;
  const orderId = order.order_id ?? order.id;
  if (order.customer_id != null && order.customer_id !== "" && orderId != null && orderId !== "") {
    return `customer:${String(order.customer_id)}:order:${String(orderId)}`;
  }
  return orderId == null || orderId === "" ? "" : `legacy:${String(orderId)}`;
}

/** Match records that carry different generations of the same identity fields. */
export function sameOrderIdentity(left, right) {
  if (!left || !right) return false;
  if (left.aggregate_id != null && right.aggregate_id != null) {
    return String(left.aggregate_id) === String(right.aggregate_id);
  }
  const leftId = left.order_id ?? left.id;
  const rightId = right.order_id ?? right.id;
  if (left.customer_id != null && right.customer_id != null && leftId != null && rightId != null) {
    return String(left.customer_id) === String(right.customer_id) && String(leftId) === String(rightId);
  }
  return orderIdentity(left) === orderIdentity(right);
}
