from shop.order import Order


def payload(order: Order) -> str:
    """The body POSTed to partners' order webhooks."""
    return order.to_json()


def summary_line(order: Order) -> str:
    return f"Order {order.order_id}: {order.subtotal} cents"
