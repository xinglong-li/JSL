import jax.numpy as jnp
from jax import random, tree_leaves, tree_map
from jax import nn 

from functools import partial
from matplotlib import pyplot as plt
import optax
from sklearn.preprocessing import PolynomialFeatures
from jsl.experimental.seql.agents import eekf_agent
from jsl.experimental.seql.agents.bayesian_lin_reg_agent import bayesian_reg


from jsl.experimental.seql.agents.bfgs_agent import bfgs_agent
from jsl.experimental.seql.agents.blackjax_nuts_agent import blackjax_nuts_agent
from jsl.experimental.seql.agents.lbfgs_agent import lbfgs_agent
from jsl.experimental.seql.agents.sgd_agent import sgd_agent
from jsl.experimental.seql.agents.sgmcmc_sgld_agent import sgld_agent
from jsl.experimental.seql.environments.base import make_evenly_spaced_x_sampler, make_random_poly_classification_environment, make_random_poly_regression_environment
from jsl.experimental.seql.experiments.bimodal_reg_demo import loglikelihood_fn
from jsl.experimental.seql.experiments.plotting import plot_classification_2d, plot_regression_posterior_predictive
from jsl.experimental.seql.utils import binary_cross_entropy, classification_loss, mse, train
from jsl.nlds.base import NLDS


def fz(x): return x
def fx(w, x): 
    return (x @ w)[None, ...]

def Rt(w, x): return (x @ w * (1 - x @ w))[None, None]

def model_fn(w, x):
    return x @ w

def logprior_fn(params, strength=0.2):
    leaves = tree_leaves(params)
    return -sum(tree_map(lambda x : jnp.sum(x**2), leaves)) * strength

  
def loglikelihood_fn(params, x, y, model_fn):
    logprobs = model_fn(params, x)
    return -classification_loss(y, logprobs)

def logjoint_fn(params, inputs, outputs, model_fn, strength=0.2):
    return loglikelihood_fn(params, inputs, outputs, model_fn) + logprior_fn(params, strength)

def neg_logjoint_fn(params, inputs, outputs, model_fn, strength=0.2):
    return -logjoint_fn(params, inputs, outputs, model_fn, strength)

def print_accuracy(logprobs, ytest):
    ytest_ = jnp.squeeze(ytest)
    predictions = jnp.where(logprobs > jnp.log(0.5), 1, 0)
    print("Accuracy: ", jnp.mean(jnp.argmax(predictions, axis=-1) == ytest_))


def callback_fn(agent, env, agent_name, **kwargs):

    print_accuracy(kwargs["preds"][0], kwargs["Y_test"])

    if "subplot_idx" not in kwargs and kwargs["t"] not in kwargs["timesteps"]:
        return
    elif "subplot_idx" not in kwargs:
        subplot_idx = kwargs["timesteps"].index(kwargs["t"]) + kwargs["idx"] * kwargs["ncols"] + 1
    else:
        subplot_idx = kwargs["subplot_idx"]


    ax = kwargs["fig"].add_subplot(kwargs["nrows"],
                                   kwargs["ncols"],
                                   subplot_idx)

    belief = kwargs["belief_state"]
    
    poly = PolynomialFeatures(kwargs["degree"])
    grid = poly.fit_transform(jnp.mgrid[-10:10:100j, -10:10:100j].reshape((2, -1)).T)
    
    grid_preds = nn.softmax(agent.predict(belief, grid)[0], axis=-1)

    
    ax = kwargs["fig"].add_subplot(kwargs["nrows"],
                                   kwargs["ncols"],
                                   subplot_idx)

    belief = kwargs["belief_state"]

    plot_classification_2d(ax,
                           env,
                           grid,
                           grid_preds,
                           kwargs["t"])

    if "title" in kwargs:
        ax.set_title(kwargs["title"], fontsize=32)
    else:
        ax.set_title("t={}".format(kwargs["t"]), fontsize=32)

    plt.tight_layout()
    plt.savefig("jaks.png")

def initialize_params(agent_name, **kwargs):
    nfeatures = kwargs["nfeatures"]
    mu0 = jnp.zeros((nfeatures, 2))
    if agent_name == "bayes" or  agent_name == "eekf":
        mu0 = jnp.zeros((nfeatures, 2))
        Sigma0 = jnp.eye(nfeatures)
        initial_params = (mu0, Sigma0)
    else:
        initial_params = (mu0,)

    return initial_params

def sweep(agents, env, train_batch_size, ntrain, 
          nsteps, figsize=(56, 48), **init_kwargs):

    batch_agents_included = "batch_agents" in init_kwargs

    nrows = len(agents)
    ncols = len(init_kwargs["timesteps"]) + int(batch_agents_included)
    fig, big_axes = plt.subplots(nrows=nrows,
                                ncols=1,
                                figsize=figsize)


    for idx, (big_ax, (agent_name, agent)) in enumerate(zip(big_axes, agents.items())):
        
        params = initialize_params(agent_name, **init_kwargs)
        belief = agent.init_state(*params)


        big_ax.set_title(agent_name.upper(), fontsize=36, y=1.2)
        # Turn off axis lines and ticks of the big subplot 
        # obs alpha is 0 in RGBA string!
        big_ax.tick_params(labelcolor=(1.,1.,1., 0.0), 
                           top='off',
                           bottom='off',
                           left='off',
                           right='off')
        # removes the white frame
        big_ax._frameon = False
        
        partial_callback = lambda **kwargs: callback_fn(agent,
                                                        env(train_batch_size),
                                                        agent_name,
                                                        fig=fig,
                                                        nrows=nrows,
                                                        ncols=ncols,
                                                        idx=idx,
                                                        **init_kwargs,
                                                        **kwargs)
        train(belief, agent, env(train_batch_size),
              nsteps=nsteps, callback=partial_callback)

        if batch_agents_included:
            batch_agent = init_kwargs["batch_agents"][agent_name]
            partial_callback = lambda **kwargs: callback_fn(batch_agent,
                                                    env(ntrain),
                                                    agent_name,
                                                    fig=fig,
                                                    nrows=nrows,
                                                    ncols=ncols,
                                                    idx=idx,
                                                    title="Batch Agent",
                                                    subplot_idx=(idx+1) * ncols,
                                                    **init_kwargs,
                                                    **kwargs)
            train(belief, batch_agent, env(ntrain),
                nsteps=1, callback=partial_callback)
    plt.savefig("ajsk.png")


def main():
    key = random.PRNGKey(0)
    
    min_val, max_val = -3, 3
    x_test_generator = make_evenly_spaced_x_sampler(max_val,
                                                    use_bias=False,
                                                    min_val=min_val)

    degree = 3
    ntrain, ntest = 50, 50
    train_batch_size = 5
    nfeatures, nclasses = 2, 2

    env_key, nuts_key, sgld_key = random.split(key, 3)
    obs_noise = 1.
    env = lambda batch_size: make_random_poly_classification_environment(env_key,
                                                                        degree,
                                                                        ntrain,
                                                                        ntest,
                                                                        nfeatures=nfeatures,
                                                                        nclasses=nclasses,
                                                                        obs_noise=obs_noise,
                                                                        train_batch_size=batch_size,
                                                                        test_batch_size=batch_size,
                                                                        x_train_generator=x_test_generator)
                                                    
    nsteps = 10
    buffer_size = ntrain

    input_dim = 10
    Pt = jnp.eye(input_dim) * 0.0
    P0 = jnp.eye(input_dim) * 2.0
    mu0 = jnp.zeros((input_dim,))
    nlds = NLDS(fz, fx, Pt, Rt, mu0, P0)

    eekf = eekf_agent.eekf(nlds, obs_noise=obs_noise)
    

    optimizer = optax.adam(1e-2)

    tau = 1.
    strength = obs_noise / tau
    partial_objective_fn = partial(neg_logjoint_fn, strength=strength)

    nepochs = 10

    sgd = sgd_agent(partial_objective_fn,
                    model_fn,
                    optimizer=optimizer,
                    obs_noise=obs_noise,
                    nepochs=nepochs,
                    buffer_size=buffer_size)
    
    batch_sgd = sgd_agent(partial_objective_fn,
                        model_fn,
                        optimizer=optimizer,
                        obs_noise=obs_noise,
                        buffer_size=buffer_size,
                        nepochs=nepochs*nsteps)

    nsamples, nwarmup = 200, 100
    nuts = blackjax_nuts_agent(nuts_key,
                                loglikelihood_fn,
                                model_fn,
                                nsamples=nsamples,
                                nwarmup=nwarmup,
                                obs_noise=obs_noise,
                                buffer_size=buffer_size)

    
    batch_nuts = blackjax_nuts_agent(nuts_key,
                                loglikelihood_fn,
                                model_fn,
                                nsamples=nsamples*nsteps,
                                nwarmup=nwarmup,
                                obs_noise=obs_noise,
                                buffer_size=buffer_size)

    partial_logprob_fn = partial(loglikelihood_fn,
                                 model_fn=model_fn)
    dt = 1e-4
    sgld = sgld_agent(sgld_key,
                    partial_logprob_fn,
                    logprior_fn,
                    model_fn,
                    dt = dt,
                    batch_size=train_batch_size,
                    nsamples=nsamples,
                    obs_noise=obs_noise,
                    buffer_size=buffer_size)

    dt = 1e-5
    batch_sgld = sgld_agent(sgld_key,
                            partial_logprob_fn,
                            logprior_fn,
                            model_fn,
                            dt = dt,
                            batch_size=train_batch_size,
                            nsamples=nsamples*nsteps,
                            obs_noise=obs_noise,
                            buffer_size=buffer_size)

    bfgs = bfgs_agent(partial_objective_fn,
                      obs_noise=obs_noise,
                      buffer_size=buffer_size)
    
    batch_bfgs = bfgs_agent(partial_objective_fn,
                    obs_noise=obs_noise,
                    buffer_size=buffer_size)

    lbfgs = lbfgs_agent(partial_objective_fn,
                        obs_noise=obs_noise,
                        history_size=buffer_size)

    batch_lbfgs = lbfgs_agent(partial_objective_fn,
                    obs_noise=obs_noise,
                    history_size=buffer_size)

    agents = {
              "sgd": sgd,
              "bfgs": bfgs,
              "lbfgs": lbfgs,
              "nuts": nuts,
              "sgld":sgld,
              }
    
    
    batch_agents = {
                    "eekf": eekf,
                    "sgd": batch_sgd,
                    "nuts": batch_nuts,
                    "sgld": batch_sgld,
                    "bfgs": batch_bfgs,
                    "lbfgs": batch_lbfgs
                    }
        
    sweep(agents, env, train_batch_size,
          ntrain, nsteps,
          nfeatures=input_dim,
          obs_noise=obs_noise,
          batch_agents=batch_agents,
          min_val=min_val,
          max_val=max_val,
          timesteps=list(range(nsteps)),
          degree=degree)


if __name__ == "__main__":
    main()