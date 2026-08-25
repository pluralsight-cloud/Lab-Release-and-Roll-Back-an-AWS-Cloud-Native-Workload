def lambda_handler(event, context):
    """Represent the deterministic failure planted in orders version 2."""
    raise RuntimeError("Simulated v2 order-processing failure.")
