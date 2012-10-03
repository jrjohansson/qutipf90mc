import numpy as np
from qutip import *
import qutraj_run as qtf90

# Working precision
wpr = dtype(float64)
wpc = dtype(complex64)

def mcsolve_f90(H,psi0,tlist,c_ops,e_ops,ntraj=500,
        options=Odeoptions(),states_as_kets=True,sparse_dms=True):
    """
    Monte-Carlo wave function solver with fortran 90 backend.
    Usage is identical to qutip.mcsolve, for problems without explicit
    time-dependence, and with some optional input:

    Parameters
    ----------
    H : qobj
        System Hamiltonian.
    psi0 : qobj
        Initial state vector
    tlist : array_like
        Times at which results are recorded.
    ntraj : int
        Number of trajectories to run.
    c_ops : array_like
        ``list`` or ``array`` of collapse operators.
    e_ops : array_like
        ``list`` or ``array`` of operators for calculating expectation values.
    options : Odeoptions
        Instance of ODE solver options.
    states_as_ket : boolean
        If True (default), e_ops = [] and options.mc_avg = True, results.states is a list of averaged ket vectors. If False, e_ops = [] and options.mc_avg = False, results.states is a list of averaged density matrices.
    sparse_dms : boolean
        If averaged density matrices are returned (states_as_kets=False, see above), they will be stored as sparse (Compressed Row Format) matrices during computation if sparse_dms = True (default), and dense matrices otherwise. Dense matrices might be preferable for smaller systems.

    Returns
    -------
    results : Odedata    
        Object storing all results from simulation.

    """
    from multiprocessing import cpu_count
    mc = _MC_class()
    if psi0.type!='ket':
        raise Exception("Initial state must be a state vector.")
    #set initial value data
    if options.tidy:
        mc.psi0=psi0.tidyup(options.atol)
    else:
        mc.psi0=psi0
    mc.dims = psi0.dims
    mc.H = H
    mc.tlist = tlist
    mc.c_ops = c_ops
    mc.e_ops = e_ops
    mc.ntraj = ntraj
    mc.options = options
    if (options.method == 'adams'):
        mc.mf = 10
    elif (options.method == 'bdf'):
        mc.mf = 22
    else:
        print 'unrecognized method for ode solver, using "adams".'
        mc.mf = 10
    if (options.num_cpus == 0):
        mc.ncpus = cpu_count()
    else:
        mc.ncpus = options.num_cpus
    mc.nprocs = mc.ncpus
    mc.run()
    return mc.sol

class _MC_class():
    def __init__(self):
        self.H = Qobj()
        self.psi0 = Qobj()
        self.tlist = []
        self.c_ops = [Qobj()]
        self.e_ops = [Qobj()]
        self.ntraj = 0
        self.options = Odeoptions()
        self.ncpus = 0
        self.nprocs = 0
        self.sol = [Odedata()]
        self.states = True
        self.mf = 10
        # If returning states, return averaged kets or density matrices?
        self.return_kets = True
        # If returning density matrices, return as sparse or dense?
        self.rho_return_sparse = True

    def parallel(self):
        from multiprocessing import Process, Queue, JoinableQueue
        ntrajs = []
        for i in range(self.ncpus):
            ntrajs.append(min(int(floor(float(self.ntraj)/self.ncpus)),
                self.ntraj-sum(ntrajs)))
        cnt = sum(ntrajs)
        while cnt<self.ntraj:
            for i in range(self.ncpus):
                ntrajs[i] += 1
                cnt+=1
                if (cnt>=self.ntraj):
                    break
        ntrajs = np.array(ntrajs)
        ntrajs = ntrajs[np.where(ntrajs>0)]
        self.nprocs = len(ntrajs)
        sols = []
        processes = []
        resq = JoinableQueue()
        print "Number of cpus:", self.ncpus
        print "Trying to start", self.nprocs, "process(es)."
        print "Number of trajectories for each process:"
        print ntrajs
        for i in range(self.nprocs):
            nt = ntrajs[i]
            p = Process(target=self.evolve_serial,
                    args=((resq,ntrajs[i],i),))
            p.start()
            processes.append(p)
        resq.join()
        cnt = 0
        while True:
            try:
                sols.append(resq.get())
                resq.task_done()
                cnt += 1
                if (cnt >= self.nprocs): break
            except KeyboardInterrupt:
                break
            except:
                pass
        resq.join()
        for proc in processes:
            try:
                proc.join()
            except KeyboardInterrupt:
                print("Cancel thread on keyboard interrupt")
                proc.terminate()
                proc.join()
        resq.close()
        return sols

    def run(self):
        if (self.c_ops == []):
            # force one trajectory if no collapse operators
            self.ntraj=1
        if (self.e_ops == []):
            self.states=True
        else:
            self.states=False
        # run in paralell
        sols = self.parallel()
        # construct Odedata object
        self.sol = Odedata()
        self.sol.ntraj=self.ntraj
        self.sol.num_collapse=size(self.c_ops)
        self.sol.num_expect=size(self.e_ops)
        self.sol.solver='Fortran 90 Monte Carlo Solver'
        self.sol.times = self.tlist
        # gather data
        if (self.states):
            if (self.options.mc_avg):
                # collect states, averaged over trajectories
                self.sol.states = sols[0].states
                for j in range(1,self.nprocs):
                    self.sol.states += sols[j].states
                self.sol.states = self.sol.states/self.nprocs
                # convert to list to be consistent with qutip mcsolve
                self.sol.states = list(self.sol.states)
            else:
                # collect states, all trajectories
                self.sol.states = np.concatenate([sols[j].states 
                    for j in range(self.nprocs)])
        else:
            if (self.options.mc_avg):
                # collect expectation values, averaged
                self.sol.expect = sols[0].expect
                for i in range(size(self.e_ops)):
                    for j in range(1,self.nprocs):
                        self.sol.expect[i] += sols[j].expect[i]
                    self.sol.expect[i] = self.sol.expect[i]/self.nprocs
            else:
                # collect expectation values, all trajectories
                #self.sol.expect = sols[0].expect
                self.sol.expect = np.concatenate([sols[j].expect 
                    for j in range(self.nprocs)])
                # convert to list to be consistent with qutip mcsolve
                self.sol.expect = list(self.sol.expect)

    def evolve_serial(self,args):
        # run ntraj trajectories for one process via fortran
        # get args
        queue,ntraj,instanceno = args 
        # initialize the problem in fortran
        _init_tlist(self.tlist)
        _init_psi0(self.psi0)
        _init_hamilt(self.H,self.c_ops)
        if (self.c_ops != []):
            _init_c_ops(self.c_ops)
        if (self.e_ops != []):
            _init_e_ops(self.e_ops)
        # set options
        qtf90.qutraj_run.ntraj = ntraj
        qtf90.qutraj_run.mc_avg = self.options.mc_avg
        qtf90.qutraj_run.init_odedata(self.psi0.shape[0],
                self.options.atol,self.options.rtol,mf=self.mf)
        # set optional arguments
        qtf90.qutraj_run.order = self.options.order
        qtf90.qutraj_run.nsteps = self.options.nsteps
        qtf90.qutraj_run.first_step = self.options.first_step
        qtf90.qutraj_run.min_step = self.options.min_step
        qtf90.qutraj_run.max_step = self.options.max_step
        qtf90.qutraj_run.norm_steps=self.options.norm_steps
        qtf90.qutraj_run.norm_tol=self.options.norm_tol
        # how to return solution
        qtf90.qutraj_run.return_kets = self.return_kets
        qtf90.qutraj_run.rho_return_sparse = self.rho_return_sparse
        #run
        qtf90.qutraj_run.evolve(self.states,instanceno)
        # construct Odedata instance
        sol = Odedata()
        if (self.states):
            sol.states = self.get_states(size(self.tlist),ntraj)
        else:
            sol.expect = self.get_expect(size(self.tlist),size(self.e_ops))
        # put to queue
        queue.put(sol)
        #queue.put('STOP')
        #deallocate stuff
        #finalize()
        return

    # Routines for retrieving data data from fortran
    def get_states(self,nstep,ntraj):
        from scipy.sparse import csr_matrix
        if (self.options.mc_avg):
            states=np.array([Qobj()]*nstep)
            if (self.return_kets):
                # averaged kets
                for i in range(nstep):
                    states[i] = Qobj(matrix(
                        qtf90.qutraj_run.sol[0,0,i,:]).transpose(),
                        dims=self.dims)
            elif (self.rho_return_sparse):
                # averaged sparse density matrices
                for i in range(nstep):
                    qtf90.qutraj_run.get_rho_sparse(i+1)
                    val = qtf90.qutraj_run.csr_val
                    col = qtf90.qutraj_run.csr_col-1
                    ptr = qtf90.qutraj_run.csr_ptr-1
                    m = qtf90.qutraj_run.csr_nrows
                    k = qtf90.qutraj_run.csr_ncols
                    states[i] = Qobj(csr_matrix((val,col,ptr),
                        (m,k)).toarray(),
                        dims=self.dims)
            else:
                # averaged dense density matrices
                for i in range(nstep):
                    states[i] = Qobj(qtf90.qutraj_run.sol[0,i,:,:],
                        dims=self.dims)
        else:
            # all trajectories as kets
            states=np.array([np.array([Qobj()]*nstep)]*ntraj)
            for traj in range(ntraj):
                for i in range(nstep):
                    states[traj][i] = Qobj(matrix(
                        qtf90.qutraj_run.sol[0,traj,i,:]).transpose(),
                        dims=self.dims)
        return states
    def get_expect(self,nstep,n_e_ops):
        if (self.options.mc_avg):
            expect=[np.array([0.+0.j]*nstep)]*n_e_ops
            for j in range(n_e_ops):
                expect[j] = qtf90.qutraj_run.sol[j,0,:,0]
        else:
            expect=np.array([[np.array([0.+0.j]*nstep)]*n_e_ops]*ntraj)
            for j in range(n_e_ops):
                expect[:,j,:] = qtf90.qutraj_run.sol[j,:,:,0]
        return expect
    def finalize():
        # not in use...
        qtf90.qutraj_run.finalize_work()
        qtf90.qutraj_run.finalize_sol()

#
# Functions to initialize the problem in fortran
#

def _init_tlist(tlist):
    Of = _realarray_to_fortran(tlist)
    qtf90.qutraj_run.init_tlist(Of,
            size(Of))
def _init_psi0(psi0):
    Of = _qobj_to_fortranfull(psi0)
    qtf90.qutraj_run.init_psi0(Of,size(Of))
def _init_hamilt(H,c_ops):
    # construct effective non-Hermitian Hamiltonian
    H_eff = H - 0.5j*sum([c_ops[i].dag()*c_ops[i]
        for i in range(len(c_ops))])
    Of = _qobj_to_fortrancsr(H_eff)
    qtf90.qutraj_run.init_hamiltonian(Of[0],Of[1],
            Of[2],Of[3],Of[4])
def _init_c_ops(c_ops):
    n = len(c_ops)
    first = True
    for i in range(n):
        Of = _qobj_to_fortrancsr(c_ops[i])
        qtf90.qutraj_run.init_c_ops(i+1,n,Of[0],Of[1],
                Of[2],Of[3],Of[4],first)
        first = False
def _init_e_ops(e_ops):
    n = len(e_ops)
    first = True
    for i in range(n):
        Of = _qobj_to_fortrancsr(e_ops[i])
        qtf90.qutraj_run.init_e_ops(i+1,n,Of[0],Of[1],
                Of[2],Of[3],Of[4],first)
        first = False
#
# Misc. converison functions
#

def _realarray_to_fortran(a):
    datad = np.array(a,dtype=wpr)
    return datad

def _complexarray_to_fortran(a):
    datad = np.array(a,dtype=wpc)
    return datad

def _qobj_to_fortranfull(A):
    datad = np.array(A.data.toarray(),dtype=wpc)
    return datad

def _qobj_to_fortrancsr(A):
    data = np.array(A.data.data,dtype=wpc)
    indices = np.array(A.data.indices)
    indptr = np.array(A.data.indptr)
    m = A.data.shape[0]
    k = A.data.shape[1]
    return data,indices+1,indptr+1,m,k

