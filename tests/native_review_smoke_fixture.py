# Synthetic review fixture only. Not imported by ERP; never merge this PR.
def synthetic_total(values):
    total = 0
    for value in values:
        total += value
    return total
