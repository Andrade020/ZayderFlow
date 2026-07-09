import numpy as np
import pandas as pd
from scipy.optimize import minimize
import warnings

# =============================================================================
# CONFIGURAÇÕES INICIAIS (facilmente ajustáveis)
# =============================================================================
SEED = 42
N_CENARIOS = 10000       # número de cenários Monte Carlo
T_DIAS = 252             # dias úteis (1 ano)
RF = 0.0                 # taxa livre de risco para Sharpe

# Três ativos com preço inicial, retorno esperado anual e volatilidade anual
ATIVOS = [
    {"nome": "Ativo 1", "S0": 100.0, "mu": 0.10, "sigma": 0.20},
    {"nome": "Ativo 2", "S0":  50.0, "mu": 0.12, "sigma": 0.25},
    {"nome": "Ativo 3", "S0": 200.0, "mu": 0.08, "sigma": 0.18},
]
N_ATIVOS = len(ATIVOS)

# =============================================================================
# FRENTE 1: SIMULAÇÃO MONTE CARLO (GBM) – RETORNOS LOGARÍTMICOS DIÁRIOS
# =============================================================================
def simular_retornos_gbm(ativos, N, T, seed):
    """
    Retorna lista com um array (N, T) de retornos logarítmicos diários para cada ativo.
    """
    np.random.seed(seed)
    dt = 1.0 / T
    retornos = []
    for ativo in ativos:
        mu, sigma = ativo["mu"], ativo["sigma"]
        Z = np.random.normal(0, 1, (N, T))
        drift = (mu - 0.5 * sigma**2) * dt
        difusao = sigma * np.sqrt(dt) * Z
        retornos.append(drift + difusao)
    return retornos

retornos_ativos = simular_retornos_gbm(ATIVOS, N_CENARIOS, T_DIAS, SEED)

# =============================================================================
# FRENTE 2: ESTIMAÇÃO DE PARÂMETROS ANUALIZADOS
# =============================================================================
def estimar_parametros(lista_retornos, dias_uteis):
    """
    Calcula retornos esperados, desvios e matriz de covariância anualizados.
    """
    # Empilha todos os retornos diários de cada ativo em vetores 1D
    ret_flat = [r.ravel() for r in lista_retornos]

    medias_diarias = [np.mean(r) for r in ret_flat]
    retornos_anuais = [m * dias_uteis for m in medias_diarias]

    # Matriz de covariância diária (ddof=1 por padrão no np.cov)
    dados = np.column_stack(ret_flat)
    cov_diaria = np.cov(dados, rowvar=False)
    cov_anual = cov_diaria * dias_uteis

    # Força simetria exata e garante matriz positiva semidefinida
    cov_anual = (cov_anual + cov_anual.T) / 2
    autovalores = np.linalg.eigvalsh(cov_anual)
    if np.min(autovalores) < 1e-12:
        cov_anual += 1e-12 * np.eye(len(lista_retornos))

    vol_anuais = np.sqrt(np.diag(cov_anual))

    return {
        "retornos_esperados_anuais": np.array(retornos_anuais),
        "matriz_covariancia": cov_anual,
        "volatilidades_anuais": vol_anuais,
    }

params = estimar_parametros(retornos_ativos, T_DIAS)
ret_anual = params["retornos_esperados_anuais"]
cov_anual = params["matriz_covariancia"]

# =============================================================================
# FRENTE 3: OTIMIZAÇÃO DE MARKOWITZ (MVG, MÁXIMO SHARPE, FRONTEIRA)
# =============================================================================
def stats_carteira(w, rets, cov):
    ret = np.dot(w, rets)
    vol = np.sqrt(np.dot(w, np.dot(cov, w)))
    return ret, vol

def variancia_carteira(w, cov):
    return np.dot(w, np.dot(cov, w))

def sharpe_negativo(w, rets, cov, rf):
    ret, vol = stats_carteira(w, rets, cov)
    if vol == 0:
        return 1e9  # penaliza carteira degenerada
    return - (ret - rf) / vol

# Restrições: soma dos pesos = 1, long-only (0 ≤ w_i ≤ 1)
restricao_soma = {"type": "eq", "fun": lambda w: np.sum(w) - 1}
limites = tuple((0, 1) for _ in range(N_ATIVOS))
w_inicial = np.ones(N_ATIVOS) / N_ATIVOS

# --- Carteira de Mínima Variância Global (MVG) ---
opt_mvg = minimize(variancia_carteira, w_inicial, args=(cov_anual,),
                   method="SLSQP", bounds=limites, constraints=restricao_soma)
w_mvg = opt_mvg.x
ret_mvg, vol_mvg = stats_carteira(w_mvg, ret_anual, cov_anual)

# --- Carteira de Máximo Índice Sharpe ---
opt_sharpe = minimize(sharpe_negativo, w_inicial, args=(ret_anual, cov_anual, RF),
                      method="SLSQP", bounds=limites, constraints=restricao_soma)
w_sharpe = opt_sharpe.x
ret_sharpe, vol_sharpe = stats_carteira(w_sharpe, ret_anual, cov_anual)
sharpe_max = (ret_sharpe - RF) / vol_sharpe

# --- Fronteira Eficiente (variando retorno alvo) ---
ret_min = np.min(ret_anual)
ret_max = np.max(ret_anual)
alvos = np.linspace(ret_min, ret_max, 50)
pesos_fronteira = []
rets_fronteira = []
vols_fronteira = []

for alvo in alvos:
    restricao_ret = {"type": "eq", "fun": lambda w, r=alvo: np.dot(w, ret_anual) - r}
    opt = minimize(variancia_carteira, w_inicial, args=(cov_anual,),
                   method="SLSQP", bounds=limites,
                   constraints=[restricao_soma, restricao_ret])
    if opt.success:
        w_opt = opt.x
        ret_opt, vol_opt = stats_carteira(w_opt, ret_anual, cov_anual)
        pesos_fronteira.append(w_opt)
        rets_fronteira.append(ret_opt)
        vols_fronteira.append(vol_opt)

# =============================================================================
# RESULTADOS CONSOLIDADOS
# =============================================================================
print("=" * 70)
print("SIMULAÇÃO DE MONTE CARLO + OTIMIZAÇÃO DE MARKOWITZ (3 ATIVOS)")
print("=" * 70)
print(f"\nParâmetros anuais estimados a partir de {N_CENARIOS} cenários de {T_DIAS} dias úteis:\n")
for i, at in enumerate(ATIVOS):
    print(f"{at['nome']:12s} retorno esperado: {ret_anual[i]:.4f}   volatilidade: {params['volatilidades_anuais'][i]:.4f}")

print("\nMatriz de covariância anual:")
print(pd.DataFrame(cov_anual, columns=[a["nome"] for a in ATIVOS], index=[a["nome"] for a in ATIVOS]))

print("\n--- Carteira de Mínima Variância Global (MVG) ---")
for n, w in zip([a["nome"] for a in ATIVOS], w_mvg):
    print(f"  {n}: {w:.4f}")
print(f"  Retorno esperado: {ret_mvg:.4f}   Volatilidade: {vol_mvg:.4f}")

print(f"\n--- Carteira de Máximo Índice Sharpe (rf = {RF}) ---")
for n, w in zip([a["nome"] for a in ATIVOS], w_sharpe):
    print(f"  {n}: {w:.4f}")
print(f"  Retorno esperado: {ret_sharpe:.4f}   Volatilidade: {vol_sharpe:.4f}   Sharpe: {sharpe_max:.4f}")

print(f"\nFronteira eficiente: {len(rets_fronteira)} carteiras geradas.")
print("(Pesos, retornos e volatilidades disponíveis nas listas 'pesos_fronteira', 'rets_fronteira', 'vols_fronteira')")

# Objeto final para uso programático
resultado_markowitz = {
    "ativos": ATIVOS,
    "simulacao": {"N_cenarios": N_CENARIOS, "T_dias": T_DIAS},
    "parametros": params,
    "mvg": {"pesos": w_mvg, "retorno": ret_mvg, "volatilidade": vol_mvg},
    "max_sharpe": {"pesos": w_sharpe, "retorno": ret_sharpe,
                   "volatilidade": vol_sharpe, "sharpe": sharpe_max, "rf": RF},
    "fronteira_eficiente": {
        "pesos": np.array(pesos_fronteira),
        "retornos": np.array(rets_fronteira),
        "volatilidades": np.array(vols_fronteira),
    },
}
