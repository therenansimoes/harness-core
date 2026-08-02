"""Test-driven automated program repair by AST mutation search.

No hint comments are read, no bug text is looked up. The agent only ever
sees: (a) the source code, (b) a numeric test score ("N/M tests pass").
It generates a pool of candidate one-token mutations (arithmetic operator
flip, comparison operator flip, or swapping a returned local name for a
sibling local name), tries each, and greedily keeps whichever mutation
raises the test score, repeating until the suite is green or the pool is
exhausted. This is the actual "derive the fix" mechanism the gen1 feedback
demanded — gen1/v3's stub agent instead grepped for the literal comment
"# BUG: should be a + b", which meant its verifier only ever measured a
str.replace calibrated on its own planted answer.
"""
import ast
import copy

BINOP_CLASSES = [ast.Add, ast.Sub, ast.Mult, ast.Div]
CMPOP_CLASSES = [ast.Lt, ast.Gt, ast.LtE, ast.GtE, ast.Eq, ast.NotEq]


def _nodes(tree):
    return list(ast.walk(tree))


def generate_mutants(src: str, max_mutants: int = 300):
    """Return a list of candidate full-file source strings, each one
    single-token mutation away from `src`."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    nodes = _nodes(tree)
    candidates = []  # (node_index, kind, alt)

    for i, node in enumerate(nodes):
        if isinstance(node, ast.BinOp) and type(node.op) in BINOP_CLASSES:
            for alt in BINOP_CLASSES:
                if alt is not type(node.op):
                    candidates.append((i, "binop", alt))
        elif isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in CMPOP_CLASSES:
            for alt in CMPOP_CLASSES:
                if alt is not type(node.ops[0]):
                    candidates.append((i, "cmpop", alt))

    for i, node in enumerate(nodes):
        if isinstance(node, ast.FunctionDef):
            local_names = {a.arg for a in node.args.args}
            for n in ast.walk(node):
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            local_names.add(t.id)
            func_node_ids = {id(n) for n in ast.walk(node)}
            for j, n in enumerate(nodes):
                if id(n) in func_node_ids and isinstance(n, ast.Return) and isinstance(n.value, ast.Name):
                    orig = n.value.id
                    for alt in local_names:
                        if alt != orig:
                            candidates.append((j, "retname", alt))

    mutants = []
    for idx, kind, alt in candidates[:max_mutants]:
        t2 = ast.parse(src)
        n2 = _nodes(t2)[idx]
        try:
            if kind == "binop":
                n2.op = alt()
            elif kind == "cmpop":
                n2.ops = [alt()]
            elif kind == "retname":
                n2.value.id = alt
            mutants.append(ast.unparse(t2))
        except Exception:
            continue
    return mutants


def repair_greedy(src: str, score_fn, max_iters: int = 40):
    """score_fn(source) -> (passed_count, total_count). Greedily accept the
    first mutant that strictly raises passed_count, repeat until all pass
    or no mutant helps. Returns (final_source, final_score, steps_taken)."""
    best_src = src
    best_score = score_fn(best_src)
    steps = 0
    for _ in range(max_iters):
        if best_score[1] and best_score[0] == best_score[1]:
            break
        mutants = generate_mutants(best_src)
        improved = False
        for m in mutants:
            score = score_fn(m)
            if score[0] > best_score[0]:
                best_src, best_score = m, score
                improved = True
                steps += 1
                break
        if not improved:
            break
    return best_src, best_score, steps
