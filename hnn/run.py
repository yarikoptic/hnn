"""File with functions and classes for running the NEURON """

# Authors: Blake Caldwell <blake_caldwell@brown.edu>
#          Sam Neymotin <samnemo@gmail.com>
#          Shane Lee

import os
import sys
from math import ceil, isclose
from contextlib import redirect_stdout
from psutil import wait_procs, process_iter, NoSuchProcess
import threading
import traceback
from queue import Queue

import nlopt
from PyQt5 import QtCore
from hnn_core import simulate_dipole, Network, MPIBackend
from hnn_core.dipole import Dipole

from .paramrw import get_output_dir


class BasicSignal(QtCore.QObject):
    """for signaling"""
    sig = QtCore.pyqtSignal()


class ObjectSignal(QtCore.QObject):
    """for returning an object"""
    sig = QtCore.pyqtSignal(object)


class QueueSignal(QtCore.QObject):
    """for synchronization"""
    qsig = QtCore.pyqtSignal(Queue, str, float)


class TextSignal(QtCore.QObject):
    """for passing text"""
    tsig = QtCore.pyqtSignal(str)


class DataSignal(QtCore.QObject):
    """for signalling data read"""
    dsig = QtCore.pyqtSignal(str, dict)


class OptDataSignal(QtCore.QObject):
    """for signalling update to opt_data"""
    odsig = QtCore.pyqtSignal(str, dict, Dipole)


class ParamSignal(QtCore.QObject):
    """for updating GUI & param file during optimization"""
    psig = QtCore.pyqtSignal(dict)


class CanvSignal(QtCore.QObject):
    """for updating main GUI canvas"""
    csig = QtCore.pyqtSignal(bool)


class ResultObj(QtCore.QObject):
    def __init__(self, data, params):
        self.data = data
        self.params = params


def _kill_list_of_procs(procs):
    """tries to terminate processes in a list before sending kill signal"""
    # try terminate first
    for p in procs:
        try:
            p.terminate()
        except NoSuchProcess:
            pass
    _, alive = wait_procs(procs, timeout=3)

    # now try kill
    for p in alive:
        p.kill()
    _, alive = wait_procs(procs, timeout=3)

    return alive


def _get_nrniv_procs_running():
    """return a list of nrniv processes running"""
    ls = []
    name = 'nrniv'
    for p in process_iter(attrs=["name", "exe", "cmdline"]):
        if name == p.info['name'] or \
                p.info['exe'] and os.path.basename(p.info['exe']) == name or \
                p.info['cmdline'] and p.info['cmdline'][0] == name:
            ls.append(p)
    return ls


def _kill_and_check_nrniv_procs():
    """handle killing any stale nrniv processess"""
    procs = _get_nrniv_procs_running()
    if len(procs) > 0:
        running = _kill_list_of_procs(procs)
        if len(running) > 0:
            pids = [str(proc.pid) for proc in running]
            print("ERROR: failed to kill nrniv process(es) %s" %
                  ','.join(pids))


def simulate(params, n_procs=None):
    """Start the simulation with hnn_core.simulate

    Parameters
    ----------
    params : dict
        The parameters

    n_procs : int | None
        The number of MPI processes requested by the user. If None, then will
        attempt to detect number of cores (including hyperthreads) and start
        parallel simulation over all of them.
    """

    # create the network from the parameter file. note, NEURON objects haven't
    # been created yet
    net = Network(params, add_drives_from_params=True)

    sim_data = {}
    # run the simulation with MPIBackend for faster completion time
    with MPIBackend(n_procs=n_procs, mpi_cmd='mpiexec'):
        record_vsoma = bool(params['record_vsoma'])
        sim_data['raw_dpls'] = simulate_dipole(net, params['N_trials'],
                                               postproc=False,
                                               record_vsoma=record_vsoma)

    # hnn-core changes this to bool, change back to int
    if isinstance(params['record_vsoma'], bool):
        params['record_vsoma'] = int(params['record_vsoma'])
    sim_data['gid_ranges'] = net.gid_ranges
    sim_data['spikes'] = net.cell_response
    sim_data['vsoma'] = net.cell_response.vsoma

    return sim_data


# based on https://nikolak.com/pyqt-threading-tutorial/
class SimThread(QtCore.QThread):
    """The SimThread class.

    Parameters
    ----------

    ncore : int
        Number of cores to run this simulation over
    params : dict
        Dictionary of params describing simulation config
    result_callback: function
        Handle to for callback to call after every sim completion
    waitsimwin : WaitSimDialog
        Handle to the Qt dialog during a simulation
    mainwin : HNNGUI
        Handle to the main application window

    Attributes
    ----------
    ncore : int
        Number of cores to run this simulation over
    params : dict
        Dictionary of params describing simulation config
    mainwin : HNNGUI
        Handle to the main application window
    opt : bool
        Whether this simulation thread is running an optimization
    killed : bool
        Whether this simulation was forcefully terminated
    """

    def __init__(self, ncore, params, result_callback, mainwin):
        QtCore.QThread.__init__(self)
        self.ncore = ncore
        self.params = params
        self.mainwin = mainwin
        self.is_optimization = self.mainwin.is_optimization
        self.baseparamwin = self.mainwin.baseparamwin
        self.result_signal = ObjectSignal()
        self.result_signal.sig.connect(result_callback)
        self.killed = False

        self.paramfn = os.path.join(get_output_dir(), 'param',
                                    self.params['sim_prefix'] + '.param')

        self.txtComm = TextSignal()
        self.txtComm.tsig.connect(self.mainwin.waitsimwin.updatetxt)

        self.param_signal = ParamSignal()
        self.param_signal.psig.connect(self.baseparamwin.updateDispParam)

        self.done_signal = TextSignal()
        self.done_signal.tsig.connect(self.mainwin.done)

    def _updatewaitsimwin(self, txt):
        """Used to write messages to simulation window"""
        self.txtComm.tsig.emit(txt)

    class _log_sim_status(object):
        """Replaces sys.stdout.write() to write message to simulation window"""
        def __init__(self, parent):
            self.out = sys.stdout
            self.parent = parent

        def write(self, message):
            self.out.write(message)
            stripped_message = message.strip()
            if not stripped_message == '':
                self.parent._updatewaitsimwin(stripped_message)

        def flush(self):
            self.out.flush()

    def stop(self):
        """Terminate running simulation"""
        _kill_and_check_nrniv_procs()
        self.killed = True

    def run(self, simlength=None):
        """Start simulation"""

        msg = ''
        banner = not self.is_optimization
        try:
            self._run(banner=banner, simlength=simlength)  # run simulation
            # update params in all windows (optimization)
        except RuntimeError as e:
            msg = str(e)
            self.done_signal.tsig.emit(msg)
            return

        if not self.is_optimization:
            self.param_signal.psig.emit(self.params)
            self.done_signal.tsig.emit(msg)

    def _run(self, banner=True, simlength=None):
        self.killed = False

        while True:
            if self.ncore == 0:
                raise RuntimeError("No cores available for simulation")

            try:
                sim_log = self._log_sim_status(parent=self)
                with redirect_stdout(sim_log):
                    sim_data = simulate(self.params, self.ncore)
                break
            except RuntimeError as e:
                if self.ncore == 1:
                    # can't reduce ncore any more
                    print(str(e))
                    self._updatewaitsimwin(str(e))
                    _kill_and_check_nrniv_procs()
                    raise RuntimeError("Simulation failed to start")

            # check if proc was killed before retrying with fewer cores
            if self.killed:
                # exit using RuntimeError
                raise RuntimeError("Terminated")

            self.ncore = ceil(self.ncore / 2)
            txt = "INFO: Failed starting simulation, retrying with %d cores" \
                % self.ncore
            print(txt)
            self._updatewaitsimwin(txt)

        # put sim_data into the val attribute of a ResultObj
        self.result_signal.sig.emit(ResultObj(sim_data, self.params))


class OptThread(SimThread):
    """The OptThread class.

    Parameters
    ----------

    ncore : int
        Number of cores to run this simulation over
    params : dict
        Dictionary of params describing simulation config
    waitsimwin : WaitSimDialog
        Handle to the Qt dialog during a simulation
    result_callback: function
        Handle to for callback to call after every sim completion
    mainwin : HNNGUI
        Handle to the main application window

    Attributes
    ----------
    ncore : int
        Number of cores to run this simulation over
    params : dict
        Dictionary of params describing simulation config
    mainwin : HNNGUI instance
        Handle to the main application window
    baseparamwin: BaseParamDialog instance
        Handle to base parameters dialog
    paramfn : str
        Full pathname of the written parameter file name
    """
    def __init__(self, ncore, params, num_steps, seed, sim_data,
                 result_callback, opt_callback, mainwin):
        super().__init__(ncore, params, result_callback, mainwin)
        self.waitsimwin = self.mainwin.waitsimwin
        self.optparamwin = self.baseparamwin.optparamwin
        self.cur_itr = 0
        self.num_steps = num_steps
        self.sim_data = sim_data
        self.result_callback = result_callback
        self.seed = seed
        self.best_step_werr = 1e9
        self.sim_running = False
        self.killed = False

        self.done_signal.tsig.connect(opt_callback)

        self.refresh_signal = BasicSignal()
        self.refresh_signal.sig.connect(self.mainwin.initSimCanvas)

        self.update_opt_data = OptDataSignal()
        self.update_opt_data.odsig.connect(sim_data.update_opt_data)

        self.update_sim_data_from_opt_data = TextSignal()
        self.update_sim_data_from_opt_data.tsig.connect(
            sim_data.update_sim_data_from_opt_data)

        self.update_opt_data_from_sim_data = TextSignal()
        self.update_opt_data_from_sim_data.tsig.connect(
            sim_data.update_opt_data_from_sim_data)

        self.update_initial_opt_data_from_sim_data = TextSignal()
        self.update_initial_opt_data_from_sim_data.tsig.connect(
            sim_data.update_initial_opt_data_from_sim_data)

        self.get_err_from_sim_data = QueueSignal()
        self.get_err_from_sim_data.qsig.connect(sim_data.get_err_wrapper)

    def run(self):
        msg = ''
        try:
            self._run()  # run optimization
        except RuntimeError as e:
            msg = str(e)

        self.done_signal.tsig.emit(msg)

    def stop(self):
        """Terminate running simulation"""
        self.sim_thread.stop()
        self.sim_thread.terminate()
        self.sim_thread.wait()
        self.killed = True
        self.done_signal.tsig.emit("Optimization terminated")

    def _run(self):
        # initialize RNG with seed from config
        nlopt.srand(self.seed)
        self.get_initial_data()

        for step in range(self.num_steps):
            self.cur_step = step

            # disable range sliders for each step once that step has begun
            self.optparamwin.toggle_enable_user_fields(step, enable=False)

            self.step_ranges = self.optparamwin.get_chunk_ranges(step)
            self.step_sims = self.optparamwin.get_sims_for_chunk(step)

            if self.step_sims == 0:
                txt = "Skipping optimization step %d (0 simulations)" % \
                    (step + 1)
                self._updatewaitsimwin(txt)
                continue

            if len(self.step_ranges) == 0:
                txt = "Skipping optimization step %d (0 parameters)" % \
                    (step + 1)
                self._updatewaitsimwin(txt)
                continue

            txt = "Starting optimization step %d/%d" % (step + 1,
                                                        self.num_steps)
            self._updatewaitsimwin(txt)
            print(txt)

            opt_results = self.run_opt_step()

            # update with optimzed params for the next round
            for var_name, new_value in zip(self.step_ranges, opt_results):
                old_value = self.step_ranges[var_name]['initial']

                # only change the parameter value if it changed significantly
                if not isclose(old_value, new_value, abs_tol=1e-9):
                    self.step_ranges[var_name]['final'] = new_value
                else:
                    self.step_ranges[var_name]['final'] = \
                        self.step_ranges[var_name]['initial']

            # push into GUI and save to param file so that next simulation
            # starts from there.
            push_values = {}
            for param_name in self.step_ranges.keys():
                push_values[param_name] = self.step_ranges[param_name]['final']
            self.baseparamwin.update_gui_params(push_values)

            # update optimization dialog window
            self.optparamwin.push_chunk_ranges(push_values)

        # update opt_data with the final best
        self.update_sim_data_from_opt_data.tsig.emit(self.paramfn)

        # check that optimization improved RMSE
        err_queue = Queue()
        self.get_err_from_sim_data.qsig.emit(err_queue, self.paramfn, self.params['tstop'])
        final_err = err_queue.get()
        if final_err > self.initial_err:
            txt = "Warning: optimization failed to improve RMSE below" + \
                  " %.2f. Reverting to old parameters." % \
                        round(self.initial_err, 2)
            self._updatewaitsimwin(txt)
            print(txt)

            initial_params = self.optparamwin.get_initial_params()
            # populate param values into GUI and save params to file
            self.baseparamwin.update_gui_params(initial_params)

            # update optimization dialog window
            self.optparamwin.push_chunk_ranges(initial_params)

            # run a full length simulation
            self.sim_thread = SimThread(self.ncore, self.params,
                                        self.result_callback,
                                        mainwin=self.mainwin)
            self.sim_running = True
            try:
                self.sim_thread.run()
                self.sim_thread.wait()
                if self.killed:
                    self.quit()
                self.sim_running = False
            except Exception:
                traceback.print_exc()
                raise RuntimeError("Failed to run final simulation. "
                                   "See previous traceback.")

    def run_opt_step(self):
        self.cur_itr = 0
        self.opt_start = self.optparamwin.get_chunk_start(self.cur_step)
        self.opt_end = self.optparamwin.get_chunk_end(self.cur_step)
        txt = 'Optimizing from [%3.3f-%3.3f] ms' % (self.opt_start,
                                                    self.opt_end)
        self._updatewaitsimwin(txt)

        # weights calculated once per step
        self.opt_weights = \
            self.optparamwin.get_chunk_weights(self.cur_step)

        # run an opt step
        algorithm = nlopt.LN_COBYLA
        self.num_params = len(self.step_ranges)
        self.opt = nlopt.opt(algorithm, self.num_params)
        opt_results = self.optimize(self.step_ranges, self.step_sims,
                                    algorithm)

        return opt_results

    def get_initial_data(self):
        # Has this simulation been run before (is there data?)
        if not self.sim_data.in_sim_data(self.paramfn):
            # run a full length simulation
            txt = "Running a simulation with initial parameter set before" + \
                " beginning optimization."
            self._updatewaitsimwin(txt)
            print(txt)

            self.sim_thread = SimThread(self.ncore, self.params,
                                        self.result_callback,
                                        mainwin=self.mainwin)
            self.sim_running = True
            try:
                self.sim_thread.run()
                self.sim_thread.wait()
                if self.killed:
                    self.quit()
                self.sim_running = False
            except Exception:
                traceback.print_exc()
                raise RuntimeError("Failed to run initial simulation. "
                                   "See previous traceback.")

            # results are in self.sim_data now

        # store the initial fit for display in final dipole plot as
        # black dashed line.
        self.update_opt_data_from_sim_data.tsig.emit(self.paramfn)
        self.update_initial_opt_data_from_sim_data.tsig.emit(self.paramfn)
        err_queue = Queue()
        self.get_err_from_sim_data.qsig.emit(err_queue, self.paramfn, self.params['tstop'])
        self.initial_err = err_queue.get()

    def opt_sim(self, new_params, grad=0):
        txt = "Optimization step %d, simulation %d" % (self.cur_step + 1,
                                                       self.cur_itr + 1)
        self._updatewaitsimwin(txt)
        print(txt)

        # Prepare a dict of parameters for this simulation to populate in GUI
        opt_params = {}
        for param_name, param_value in zip(self.step_ranges.keys(),
                                           new_params):
            if param_value >= self.step_ranges[param_name]['minval'] and \
                    param_value <= self.step_ranges[param_name]['maxval']:
                opt_params[param_name] = param_value
            else:
                # This test is not strictly necessary with COBYLA, but in
                # case the algorithm is changed at some point in the future
                print('INFO: optimization chose '
                      '%.3f for %s outside of [%.3f-%.3f].'
                      % (param_value, param_name,
                         self.step_ranges[param_name]['minval'],
                         self.step_ranges[param_name]['maxval']))
                return 1e9  # invalid param value -> large error

        # populate param values into GUI and save params to file
        self.baseparamwin.update_gui_params(opt_params)

        # run the simulation, but stop at self.opt_end
        self.sim_thread = SimThread(self.ncore, self.params,
                                    self.result_callback,
                                    mainwin=self.mainwin)

        self.sim_running = True
        try:
            self.sim_thread.run(simlength=self.opt_end)
            self.sim_thread.wait()
            if self.killed:
                self.quit()
            self.sim_running = False
        except Exception:
            traceback.print_exc()
            raise RuntimeError("Failed to run simulation. "
                               "See previous traceback.")

        # calculate wRMSE for all steps
        werr = self.sim_data.get_werr(self.paramfn, self.opt_weights,
                                      self.opt_end, tstart=self.opt_start)
        txt = "Weighted RMSE = %f" % werr
        print(txt)
        self._updatewaitsimwin(os.linesep + 'Simulation finished: ' + txt +
                               os.linesep)

        # save params numbered by cur_itr
        # data_dir = op.join(get_output_dir(), 'data')
        # sim_dir = op.join(data_dir, self.params['sim_prefix'])
        # param_out = os.path.join(sim_dir, 'step_%d_sim_%d.param' %
        #                          (self.cur_step, self.cur_itr))
        # write_legacy_paramf(param_out, self.params)

        if werr < self.best_step_werr:
            self._updatewaitsimwin("new best with RMSE %f" % werr)

            self.update_opt_data_from_sim_data.tsig.emit(self.paramfn)

            self.best_step_werr = werr
            # save best param file
            # param_out = os.path.join(sim_dir, 'step_%d_best.param' %
            #                          self.cur_step)
            # write_legacy_paramf(param_out, self.params)

        if self.cur_itr == 0 and self.cur_step > 0:
            # Update plots for the first simulation only of this step
            # (best results from last round). Skip the first step because
            # there are no optimization results to show yet.
            self.refresh_signal.sig.emit()  # redraw with updated RMSE

        self.cur_itr += 1

        return werr

    def optimize(self, params_input, num_sims, algorithm):
        opt_params = []
        lb = []
        ub = []

        for param_name in params_input.keys():
            upper = params_input[param_name]['maxval']
            lower = params_input[param_name]['minval']
            if upper == lower:
                continue

            ub.append(upper)
            lb.append(lower)
            opt_params.append(params_input[param_name]['initial'])

        if algorithm == nlopt.G_MLSL_LDS or algorithm == nlopt.G_MLSL:
            # In case these mixed mode (global + local) algorithms are
            # used in the future
            local_opt = nlopt.opt(nlopt.LN_COBYLA, self.num_params)
            self.opt.set_local_optimizer(local_opt)

        self.opt.set_lower_bounds(lb)
        self.opt.set_upper_bounds(ub)

        # minimize the wRMSE returned by self.opt_sim
        self.opt.set_min_objective(self.opt_sim)
        self.opt.set_xtol_rel(1e-4)
        self.opt.set_maxeval(num_sims)

        # start the optimization: run self.runsim for # iterations in num_sims
        opt_results = self.opt.optimize(opt_params)

        return opt_results
