"""Opt-in transaction boundary for short, database-backed DRF requests."""

from django.db import transaction


class RLSRequestTransactionMixin:
    """For non-streaming DB endpoints; defer network side effects until commit."""

    def dispatch(self, request, *args, **kwargs):
        # DRF authenticates inside dispatch. With transaction pooling, context
        # and every protected query must use that same server transaction.
        with transaction.atomic():
            response = super().dispatch(request, *args, **kwargs)
            # DRF catches API exceptions itself; don't commit a partial mutation
            # just because the exception was converted into a response.
            if getattr(response, "exception", False):
                transaction.set_rollback(True)
            return response
