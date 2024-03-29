'''
|============================================= vt.py ==============================================|
|                                                                                                  |
|   Property of Daniel Sturdivant. Unauthorized copying of this file via any medium would be       |
|   super sad and unfortunate for me. Proprietary and confidential.                                |
|                                                                                                  |
| ------------------------------------------------------------------------------------------------ |
|                                                                                                  |
|   @file     charlizard/navigators/vt.py                                                          |
|   @brief    Vector tracking (VDFLL + PLL) architecture.                                          |
|   @author   Daniel Sturdivant <sturdivant20@gmail.com>                                           |
|   @date     February 2024                                                                        |
|                                                                                                  |
|==================================================================================================|
'''

import numpy as np
from scipy.linalg import solve_discrete_are, solve_continuous_are, inv, norm, cholesky
from log_utils import default_logger as _log

from charlizard.navigators.structures import VDFLLConfig
from charlizard.models.discriminator import *
from charlizard.models.lock_detectors import *
from charlizard.models.correlators import Correlators

from navsim.error_models.clock import NavigationClock, get_clock_allan_variance_values

from navtools.conversions.coordinates import ecef2enuDcm, ecef2enu, ecef2enuv, ecef2lla
from navtools.common import compute_range_and_unit_vector, compute_range_rate
from navtools.constants import *

# easy conversions
R2D =  180 / np.pi
D2R = np.pi / 180
LLA_R2D = np.array([R2D, R2D, 1.0], dtype=float)
I3 = np.eye(3)
Z33 = np.zeros((3,3))
Z32 = np.zeros((3,2))
Z23 = np.zeros((2,3))

class VectorTrack:
  @property
  def extract_states(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    # update ENU state
    self.vel = self._C_e_n @ self.rx_state[3:6]
    self.pos = self._C_e_n @ (self.rx_state[0:3] - self._ecef0)
    self.lla = ecef2lla(self.rx_state[0:3]) * LLA_R2D
    self.clk = self.rx_state[-2:]
    return self.pos, self.vel, self.lla, self.clk
  
  @property
  def extract_stds(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    # extract ENU standard deviations
    vel = np.sqrt(np.diag(self._C_e_n @ self.rx_cov[3:6,3:6] @ self._C_n_e))
    pos = np.sqrt(np.diag(self._C_e_n @ self.rx_cov[6:9,6:9] @ self._C_n_e))
    clk = np.sqrt(np.diag(self.rx_cov[-2:,-2:]))
    return pos, vel, clk
  
  @property
  def extract_dops(self) -> tuple[float, float, float, float, float, int]:
    n = self._range_unit_vectors.shape[0]
    i1 = np.ones(n)
    H = np.block([self._range_unit_vectors, i1[:,None]])
    dop = inv(H.T @ H)
    dop[:3,:3] = self._C_e_n @ dop[:3,:3] @ self._C_n_e
    gdop = np.sqrt(dop.trace())
    pdop = np.sqrt(dop[:3,:3].trace())
    hdop = np.sqrt(dop[:2,:2].trace())
    vdop = np.sqrt(dop[2,2])
    tdop = np.sqrt(dop[3,3])
    return gdop, pdop, hdop, vdop, tdop, n
  
  def __init__(self, config: VDFLLConfig):
    # grab config
    self._is_signal_level = config.is_signal_level
    self._tap_spacing = config.tap_spacing
    self._innovation_stdev = config.innovation_stdev
    if config.clock_type is None:
      self._clock_config = NavigationClock(h0=0.0, h1=0.0, h2=0.0)
    else:
      self._clock_config = get_clock_allan_variance_values(config.clock_type)
    
    # signal settings
    # sig_config = config.signal_config.get('gps'.casefold())
    # self._chip_width = SPEED_OF_LIGHT / sig_config.fchip_data
    # self._wavelength = SPEED_OF_LIGHT / sig_config.fcarrier
    self._chip_width = SPEED_OF_LIGHT / 1.023e6
    self._wavelength = SPEED_OF_LIGHT / 1575.42e6
    
    # correlators
    self._correlators = Correlators(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    
    # cn0 estimation config
    self.cn0 = config.cn0
    self._cn0_counter = 0
    self._cn0_buffer_len = config.cn0_buffer_len
    self._cn0_ip_buffer = []
    self._cn0_qp_buffer = []
    
    # initialize kalman filter
    self._is_cov_initialized = False
    self._order = config.order
    self.T = config.T
    if config.order == 2:
      self.rx_state = np.hstack((config.pos, config.vel, config.clock_bias, config.clock_drift))
      self.rx_cov = np.diag([2.0,2.0,2.0,0.05,0.05,0.05,2.0,0.1])
    elif config.order == 3:
      self.rx_state = np.hstack((config.pos, config.vel, 0,0,0, config.clock_bias, config.clock_drift))
      self.rx_cov = np.diag([2.0,2.0,2.0,0.05,0.05,0.05,0.01,0.01,0.01,2.0,0.1])
    else:
      _log.Error("VectorTrack::init -> invalid filter order specified. Must be 2 or 3.")
      raise ValueError()
    self.__generate_A(config.order)
    self.__generate_Q(config.order, config.process_noise_stdev)
    
    # generate ecef-2-enu rotation matrix
    self._lla0 = ecef2lla(config.pos)
    self._ecef0 = config.pos
    self._C_e_n = ecef2enuDcm(self._lla0)
    self._C_n_e = self._C_e_n.T
  

#--------------------------------------------------------------------------------------------------#
  #! === Time Update (prediction) ===
  def time_update(self):
    # assumed constant T
    self.rx_state = self._A @ self.rx_state
    self.rx_cov = self._A @ self.rx_cov @ self._A.T + self._Q
    return self.rx_state, self.rx_cov
  
  #! === Measurement Update (correction) ===
  def measurement_update(self, meas_prange: np.ndarray, meas_prange_rate: np.ndarray):
    # observation and covariance matrix
    self.__estimate_cn0()
    self.__generate_C(self._order)
    self.__generate_R()
    
    # compute initial covariance if necessary
    if not self._is_cov_initialized:
      self.__initialize_covariance()
      self._is_cov_initialized = True
      
    # measurement residuals (half chip spacing to minimize correlation between early and late)
    # chip_error = dll_error(
    #     self._correlators.IE, self._correlators.QE, self._correlators.IL, self._correlators.QL, self._tap_spacing
    #   ) * self._chip_width
    # freq_error = fll_error(
    #     self._correlators.ip1, self._correlators.ip2, self._correlators.qp1, self._correlators.qp2, self.T
    #   ) * -self._wavelength
    # dy = np.concatenate((chip_error, freq_error))
    dy = np.concatenate((meas_prange - self._pred_prange, 
                         meas_prange_rate - self._pred_prange_rate))
    
    # innovation filtering
    S = self._C @ self.rx_cov @ self._C.T + self._R
    norm_z = np.abs(dy / np.sqrt(np.diag(S))) # norm_z = np.abs(dy / np.diag(cholesky(S)))
    fault_idx = np.nonzero(norm_z < self._innovation_stdev)[0]
    self._C = self._C[fault_idx,:]
    self._R = np.diag(self._R[fault_idx,fault_idx])
    dy = dy[fault_idx]
    
    # update
    L = self.rx_cov @ self._C.T @ inv(self._C @ self.rx_cov @ self._C.T + self._R)
    I_LC = np.eye(self.rx_state.size) - L @ self._C
    self.rx_cov = (I_LC @ self.rx_cov @ I_LC.T) + (L @ self._R @ L.T)
    self.rx_state += L @ dy
    return self.rx_state, self.rx_cov
  
  #! === Predict Observables ===
  def predict_observables(self, emitter_states: dict) -> tuple[np.ndarray, np.ndarray]:
    URs = []
    UVs = []
    ranges = []
    range_rates = []

    for emitter in emitter_states.values():
      dr = emitter.pos - self.rx_state[0:3]
      dv = emitter.vel - self.rx_state[3:6]
      
      r = norm(dr)
      ur = dr / r
      v = ur @ dv
      uv = np.cross(ur, np.cross(ur, dv/r))
      
      URs.append(ur)
      UVs.append(uv)
      ranges.append(r)
      range_rates.append(v)
      
    self._pred_prange = np.array(ranges) + self.rx_state[-2]            #* add clock bias
    self._pred_prange_rate = np.array(range_rates) + self.rx_state[-1]  #* add clock drift
    self._range_unit_vectors = np.array(URs)
    self._rate_unit_vectors = np.array(UVs)
    return self._pred_prange, self._pred_prange_rate
  
  #! === Predict NCO Frequencies ===


#--------------------------------------------------------------------------------------------------#
  #! === Update Correlators ===
  def update_correlators(self, correlators: Correlators):
    self._correlators = correlators

  #! === Estimate CN0 ===
  def __estimate_cn0(self):
    self._cn0_ip_buffer.append(self._correlators.IP)
    self._cn0_qp_buffer.append(self._correlators.QP)
    self._cn0_counter += 1
    if self._cn0_counter == self._cn0_buffer_len:
      ip_tmp = np.array(self._cn0_ip_buffer)
      qp_tmp = np.array(self._cn0_qp_buffer)
      for i in range(self.cn0.size):
        self.cn0[i] = CN0_m2m4_estimator(
            ip_tmp[:,i], 
            qp_tmp[:,i], 
            self.cn0[i], 
            self.T
          )
        # self.cn0[i] = CN0_beaulieu_estimator(
        #     ip_tmp[:,i], 
        #     qp_tmp[:,i], 
        #     self.cn0[i], 
        #     self.T,
        #     False,
        #   )
      self._cn0_counter = 0
      self._cn0_ip_buffer = []
      self._cn0_qp_buffer = []
    
  
#--------------------------------------------------------------------------------------------------#
  #! === Initialize Receiver Covariance Matrix ===
  def __initialize_covariance(self):
    # this allows for quick convergence
    # self.rx_cov = 10*solve_discrete_are(self._A.T, self._C.T, 0.5*(self._Q+self._Q.T), self._R)
    
    I = np.eye(self._A.shape[0])
    delta_diag_P = np.diag(self.rx_cov)

    while np.any(delta_diag_P > 1e-4):
      previous_P = self.rx_cov

      self.rx_cov = self._A @ self.rx_cov @ self._A.T + self._Q
      K = self.rx_cov @ self._C.T @ inv(self._C @ self.rx_cov @ self._C.T + self._R)
      self.rx_cov = (I - K @ self._C) @ self.rx_cov @ (I - K @ self._C).T + K @ self._R @ K.T

      delta_diag_P = np.diag(previous_P - self.rx_cov)
  
  #! === State Transition Matrix ===
  def __generate_A(self, order: int):
    clk_a = np.array([[1.0, self.T],[0.0, 1.0]])
    if order == 2:
      self._A = np.block([[ I3, self.T*I3,   Z32], 
                          [Z33,        I3,   Z32],
                          [Z23,       Z23, clk_a]])
    elif order == 3:
      self._A = np.block([[ I3, self.T*I3, 0.5*self.T**2*I3,   Z32], 
                          [Z33,        I3,        self.T*I3,   Z32], 
                          [Z33,       Z33,               I3,   Z32],
                          [Z23,       Z23,              Z23, clk_a]])
      
  #! === Process Noise Covariance ===
  def __generate_Q(self, order: int, sigma: float):
    # clock
    Sb = self._clock_config.h0 / 2
    Sd = self._clock_config.h2 * 2 * np.pi**2
    clk_q = SPEED_OF_LIGHT**2 * np.array([[Sb*self.T + Sd/3*self.T**3, Sd/2*self.T**2],
                                          [            Sd/2*self.T**2,      Sd*self.T]])
    if order == 2:
      xyz_pp = sigma**2 * self.T**3 * I3 / 3
      xyz_vv = sigma**2 * self.T * I3
      xyz_pv = sigma**2 * self.T**2 * I3 / 3
      self._Q = np.block([[xyz_pp, xyz_pv,   Z32], 
                          [xyz_pv, xyz_vv,   Z32], 
                          [   Z23,    Z23, clk_q]])
    elif order == 3:
      xyz_pp = sigma**2 * self.T**5/20 * I3
      xyz_vv = sigma**2 * self.T**3/3 * I3
      xyz_aa = sigma**2 * self.T * I3
      xyz_pv = sigma**2 * self.T**4/8 * I3
      xyz_pa = sigma**2 * self.T**3/6 * I3
      xyz_va = sigma**2 * self.T**2/2 * I3
      self._Q = np.block([[xyz_pp, xyz_pv, xyz_pa,   Z32], 
                          [xyz_pv, xyz_vv, xyz_va,   Z32], 
                          [xyz_pa, xyz_va, xyz_aa,   Z32],
                          [   Z23,    Z23,    Z23, clk_q]])
      
  #! === Observation Matrix ===
  def __generate_C(self, order: int):
    Z = np.zeros(self._range_unit_vectors.shape)
    Z1 = np.zeros(self._range_unit_vectors.shape[0])
    I1 = np.ones(self._range_unit_vectors.shape[0])
    if order == 2:
      self._C = np.block([[-self._range_unit_vectors, Z, I1[:,None], Z1[:,None]],
                          [-self._rate_unit_vectors, -self._range_unit_vectors, Z1[:,None], I1[:,None]]])
    elif order == 3:
      self._C = np.block([[-self._range_unit_vectors, Z, Z, I1[:,None], Z1[:,None]],
                          [-self._rate_unit_vectors, -self._range_unit_vectors, Z, Z1[:,None], I1[:,None]]])
      
  #! === Measurement Noise Covariance ===
  def __generate_R(self):
    self._R = np.diag( 
                np.concatenate((prange_residual_var(self.cn0, self.T, self._chip_width), 
                                prange_rate_residual_var(self.cn0, self.T, self._wavelength))) 
              )
      