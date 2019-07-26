"""
The basic example is taken from "Langtangen, Hans Petter, and Anders Logg. Solving PDEs in Python: The FEniCS
Tutorial I. Springer International Publishing, 2016."

The example code has been extended with preCICE API calls and mixed boundary conditions to allow for a Dirichlet-Neumann
coupling of two separate heat equations.

The original source code can be found on https://github.com/hplgit/fenics-tutorial/blob/master/pub/python/vol1/ft03_heat.py.

Heat equation with Dirichlet conditions. (Dirichlet problem)
  u'= Laplace(u) + f  in the unit square [0,1] x [0,1]
  u = u_C             on the coupling boundary at x = 1
  u = u_D             on the remaining boundary
  u = u_0             at t = 0
  u = 1 + x^2 + alpha*y^2 + \beta*t
  f = beta - 2 - 2*alpha

Heat equation with mixed boundary conditions. (Neumann problem)
  u'= Laplace(u) + f  in the shifted unit square [1,2] x [0,1]
  du/dn = f_N         on the coupling boundary at x = 1
  u = u_D             on the remaining boundary
  u = u_0             at t = 0
  u = 1 + x^2 + alpha*y^2 + \beta*t
  f = beta - 2 - 2*alpha
"""

from __future__ import print_function, division
from fenics import Function, SubDomain, RectangleMesh, FunctionSpace, Point, Expression, Constant, DirichletBC, \
    TrialFunction, TestFunction, File, solve, plot, lhs, rhs, grad, inner, dot, dx, ds, assemble, interpolate, project, \
    near, MeshFunction, FacetNormal, VectorFunctionSpace
from enum import Enum
import fenicsadapter.core
from errorcomputation import compute_errors
import argparse
import numpy as np


class ProblemType(Enum):
    """
    Enum defines problem type. Details see above.
    """
    DIRICHLET = 1  # Dirichlet problem
    NEUMANN = 2  # Neumann problem

class Subcyling(Enum):
    """
    Enum defines which kind of subcycling is used
    """
    NONE = 0  # no subcycling, precice_dt == fenics_dt
    MATCHING = 1  # subcycling, where fenics_dt fits into precice_dt, mod(precice_dt, fenics_dt) == 0
    NONMATCHING = 2  # subcycling, where fenics_dt does not fit into precice_dt, mod(precice_dt, fenics_dt) != 0

    # note: the modulo expressions above should be understood in an exact way (no floating point round off problems. For
    # details, see https://stackoverflow.com/questions/14763722/python-modulo-on-floats)


class ComplementaryBoundary(SubDomain):
    def __init__(self, subdomain):
        self.complement = subdomain
        SubDomain.__init__(self)

    def inside(self, x, on_boundary):
        tol = 1E-14
        if on_boundary and not self.complement.inside(x, on_boundary):
            return True
        else:
            return False


class ClosedCouplingBoundary(SubDomain):
    def inside(self, x, on_boundary):
        tol = 1E-14
        if on_boundary and near(x[0], x_coupling, tol) and not near(x[1], y_bottom, tol) and not near(x[1], y_top, tol):
            return True
        else:
            return False


class OpenCouplingBoundary(SubDomain):
    def inside(self, x, on_boundary):
        tol = 1E-14
        if on_boundary and near(x[0], x_coupling, tol):
            return True
        else:
            return False


class NoFluxBoundary(SubDomain):
    def inside(self, x, on_boundary):
        tol = 1E-14
        if not near(x[0], x_coupling, tol):
            return True
        else:
            return False


def determine_gradient(V_g, u, flux):
    """
    compute flux following http://hplgit.github.io/INF5620/doc/pub/fenics_tutorial1.1/tu2.html#tut-poisson-gradu
    :param mesh
    :param u: solution where gradient is to be determined
    :return:
    """

    w = TrialFunction(V_g)
    v = TestFunction(V_g)

    a = inner(w, v) * dx
    L = inner(grad(u), v) * dx
    solve(a == L, flux)


parser = argparse.ArgumentParser()
parser.add_argument("-d", "--dirichlet", help="create a dirichlet problem", dest='dirichlet', action='store_true')
parser.add_argument("-n", "--neumann", help="create a neumann problem", dest='neumann', action='store_true')
parser.add_argument("-g", "--gamma", help="parameter gamma to set temporal dependence of heat flux", default=0.0, type=float)
parser.add_argument("-a", "--arbitrary-coupling-interface", help="uses more general, but less exact method for interpolation on coupling interface, see https://github.com/precice/fenics-adapter/milestone/1", dest='arbitrary_coupling_interface', action='store_true')


args = parser.parse_args()


config_file_name = "precice-config.xml"  # TODO should be moved into config, see https://github.com/precice/fenics-adapter/issues/5 , in case file doesnt not exsist open will fail

# coupling parameters
if args.dirichlet:
    problem = ProblemType.DIRICHLET
    solver_name = "HeatDirichlet"
    mesh_name = "DirichletNodes"
    write_data_name = "Flux"
    read_data_name = "Temperature"
if args.neumann:
    problem = ProblemType.NEUMANN
    solver_name = "HeatNeumann"
    mesh_name = "NeumannNodes"
    write_data_name = "Temperature"
    read_data_name = "Flux"
if args.dirichlet and args.neumann:
    raise Exception("you can only choose either a dirichlet problem (option -d) or a neumann problem (option -n)")
if not (args.dirichlet or args.neumann):
    raise Exception("you have to choose either a dirichlet problem (option -d) or a neumann problem (option -n)")

# Create mesh and define function space

nx = 5
ny = 10
subcycle = Subcyling.NONE

if problem is ProblemType.DIRICHLET:
    nx = nx*3

elif problem is ProblemType.NEUMANN:
    ny = 20

if subcycle is Subcyling.NONE and not args.arbitrary_coupling_interface:
    fenics_dt = .1  # time step size
    error_tol = 10 ** -7  # Error is bounded by coupling accuracy. In theory we would obtain the analytical solution.
    interpolation_strategy = fenicsadapter.core.ExactInterpolationExpression
elif subcycle is Subcyling.NONE and args.arbitrary_coupling_interface:
    fenics_dt = .1  # time step size
    error_tol = 10 ** -3 # error low, if we do not subcycle. In theory we would obtain the analytical solution.
    interpolation_strategy = fenicsadapter.core.GeneralInterpolationExpression
    # TODO For reasons, why we currently still have a relatively high error, see milestone https://github.com/precice/fenics-adapter/milestone/1
elif subcycle is Subcyling.MATCHING:
    fenics_dt = .01  # time step size
    error_tol = 10 ** -2  # error increases. If we use subcycling, we cannot assume that we still get the exact solution.
    # TODO Using waveform relaxation, we should be able to obtain the exact solution here, as well.
elif subcycle is Subcyling.NONMATCHING:
    fenics_dt = .03  # time step size
    error_tol = 10 ** -1  # error increases. If we use subcycling, we cannot assume that we still get the exact solution.
    # TODO Using waveform relaxation, we should be able to obtain the exact solution here, as well.

alpha = 3  # parameter alpha
beta = 1.3  # parameter beta
gamma = args.gamma  # parameter gamma, dependence of heat flux on time
y_bottom, y_top = 0, 1
x_left, x_right = 0, 2
x_coupling = 1.5  # x coordinate of coupling interface

if problem is ProblemType.DIRICHLET:
    p0 = Point(x_left, y_bottom)
    p1 = Point(x_coupling, y_top)
elif problem is ProblemType.NEUMANN:
    p0 = Point(x_coupling, y_bottom)
    p1 = Point(x_right, y_top)

mesh = RectangleMesh(p0, p1, nx, ny)
V = FunctionSpace(mesh, 'P', 2)

# Define boundary condition
u_D = Expression('1 + gamma*t*x[0]*x[0] + (1-gamma)*x[0]*x[0] + alpha*x[1]*x[1] + beta*t', degree=2, alpha=alpha, beta=beta, gamma=gamma, t=0)
u_D_function = interpolate(u_D, V)
# Define flux in x direction on coupling interface (grad(u_D) in normal direction)
f_N = Expression('2 * gamma*t*x[0] + 2 * (1-gamma)*x[0] ', degree=1, gamma=gamma, t=0)
f_N_function = interpolate(f_N, V)

bcs = [DirichletBC(V, u_D, ComplementaryBoundary(ClosedCouplingBoundary()))]
# Define initial value
u_n = interpolate(u_D, V)
u_n.rename("Temperature", "")

if problem is ProblemType.DIRICHLET:
    write_field = f_N_function
    read_field = u_D_function
elif problem is ProblemType.NEUMANN:
    write_field = u_D_function
    read_field = f_N_function

precice = fenicsadapter.core.Adapter(solver_name, 0, 1, interpolation_strategy=interpolation_strategy)
precice.configure(config_file_name)
precice.set_coupling_mesh(mesh_name, mesh, OpenCouplingBoundary())
precice_dt = precice.initialize()

if precice.is_action_required(fenicsadapter.core.action_write_initial_data()):
    precice.write_block_scalar_data(write_data_name, mesh_name, write_field)
    precice.fulfilled_action(fenicsadapter.core.action_write_initial_data())

precice.initialize_data()

if precice.is_read_data_available():
    coupling_expression = precice.read_block_scalar_data(read_data_name, mesh_name)
else:
    coupling_expression = precice.create_coupling_boundary_condition(read_field)

dt = Constant(0)
dt.assign(np.min([fenics_dt, precice_dt]))

# Define variational problem
u = TrialFunction(V)
v = TestFunction(V)
f = Expression('beta + gamma * x[0] * x[0] - 2 * gamma * t - 2 * (1-gamma) - 2 * alpha', degree=2, alpha=alpha, beta=beta, gamma=gamma, t=0)
F = u * v / dt * dx + dot(grad(u), grad(v)) * dx - (u_n / dt + f) * v * dx

if problem is ProblemType.DIRICHLET:
    # apply Dirichlet boundary condition on coupling interface
    bcs.append(DirichletBC(V, coupling_expression, OpenCouplingBoundary()))
if problem is ProblemType.NEUMANN:
    # apply Neumann boundary condition on coupling interface, modify weak form correspondingly
    F += coupling_expression * v * ds

a, L = lhs(F), rhs(F)

# Time-stepping
u_np1 = Function(V)
u_np1.rename("Temperature", "")

# reference solution at t=0
u_ref = interpolate(u_D, V)
u_ref.rename("reference", " ")

temperature_out = File("out/%s.pvd" % solver_name)
ref_out = File("out/ref%s.pvd" % solver_name)
error_out = File("out/error%s.pvd" % solver_name)

# output solution and reference solution at t=0, n=0
t = 0
n = 0
print('output u^%d and u_ref^%d' % (n, n))
temperature_out << u_n
ref_out << u_ref

error_total, error_pointwise = compute_errors(u_n, u_ref, V)
error_out << error_pointwise

# set t_1 = t_0 + dt, this gives u_D^1
u_D.t = t + dt(0)  # call dt(0) to evaluate FEniCS Constant. Todo: is there a better way?
f.t = t + dt(0)

V_g = VectorFunctionSpace(mesh, 'P', 1)
flux = Function(V_g)
flux.rename("Flux", "")

# write checkpoint
if precice.is_action_required(fenicsadapter.core.action_write_iteration_checkpoint()):
    u_cp = u_n.copy(deepcopy=True)
    t_cp = t
    n_cp = n
    precice.fulfilled_action(fenicsadapter.core.action_write_iteration_checkpoint())

while precice.is_coupling_ongoing():

    # Compute solution u^n+1, use bcs u_D^n+1, u^n and coupling bcs
    solve(a == L, u_np1, bcs)

    if problem is ProblemType.DIRICHLET:
        # Dirichlet problem obtains flux from solution and sends flux on boundary to Neumann problem
        determine_gradient(V_g, u_np1, flux)
        flux_x, flux_y = flux.split()
        write_field = flux_x
    elif problem is ProblemType.NEUMANN:
        # Neumann problem obtains sends temperature on boundary to Dirichlet problem
        write_field = u_np1

    precice.write_block_scalar_data(write_data_name, mesh_name, write_field)
    precice_dt = precice.advance(dt)
    dt.assign(np.min([fenics_dt, precice_dt]))
    read_expression = precice.read_block_scalar_data(read_data_name, mesh_name)
    coupling_expression.update(read_expression)

    # checkpointing
    if precice.is_action_required(fenicsadapter.core.action_read_iteration_checkpoint()):
        # continue FEniCS computation from checkpoint
        u_n.assign(u_cp)  # set u_n to value of checkpoint
        t = t_cp
        n = n_cp
        precice.fulfilled_action(fenicsadapter.core.action_read_iteration_checkpoint())
    else:
        u_n.assign(u_np1)
        t = t + dt
        n = n + 1

    if precice.is_action_required(fenicsadapter.core.action_write_iteration_checkpoint()):
        # continue FEniCS computation with u_np1
        # update checkpoint
        u_cp.assign(u_np1)
        t_cp = t
        n_cp = n
        precice.fulfilled_action(fenicsadapter.core.action_write_iteration_checkpoint())
        # write results and chec
        u_ref = interpolate(u_D, V)
        u_ref.rename("reference", " ")
        error, error_pointwise = compute_errors(u_n, u_ref, V, total_error_tol=error_tol)
        print('n = %d, t = %.2f: L2 error on domain = %.3g' % (n, t, error))
        # output solution and reference solution at t_n+1
        print('output u^%d and u_ref^%d' % (n, n))
        temperature_out << u_n
        ref_out << u_ref
        error_out << error_pointwise

    # Update dirichlet BC
    u_D.t = t + dt(0)
    f.t = t + dt(0)

# Hold plot
precice.finalize()