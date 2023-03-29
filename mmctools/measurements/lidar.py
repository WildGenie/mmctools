"""
Data readers for remote sensing devices (e.g., 3D data)

Based on https://github.com/NWTC/datatools/blob/master/remote_sensing.py
"""
import numpy as np
import pandas as pd
import xarray as xr


def calc_xyz(df,range=None,azimuth=None,elevation=0.0,
             small_elevation_angles=False):
    """Transform scan data with range (range gate center), azimuth, and
    elevation indices to Cartesian x,y,z coordinates

    Parameters
    ----------
    range : optional
        Needed for vertical scan data without a range coordinate [m]
    azimuth : optional
        Needed for RHI scan data without an azimuth coordinate [deg]
    elevation : optional
        Needed for PPI scan data without an elevation coordinate [deg]
    small_elevation_angles : optional
        Invoke small angle approximation, i.e., cos(x)~1 and sin(x)~x
    """

    try:
        r  = df.index.get_level_values('range')
    except KeyError:
        assert (range is not None), 'need to specify constant value for `range`'
        r = range
    try:
        az = np.radians(90 - df.index.get_level_values('azimuth'))
    except KeyError:
        assert (range is not None), 'need to specify constant value for `azimuth`'
        az = np.radians(90 - azimuth)
    try:
        el = np.radians(df.index.get_level_values('elevation'))
    except KeyError:
        el = np.radians(elevation)
    if small_elevation_angles:
        x = r * np.cos(az)
        y = r * np.sin(az)
        z = r * el
    else:
        x = r * np.cos(az)* np.cos(el)
        y = r * np.sin(az)* np.cos(el)
        z = r * np.sin(el)
    return x,y,z


def estimate_mean_wind(doppler,elevation=None):
    """Estimate mean wind speed and direction given an arc scan

    Parameters
    ----------
    doppler : pd.DataFrame or pd.Series
        Radial velocity with range, azimuth, and (optionally) elevation
        indices; if a single elevation cannot be inferred from the data,
        `elevation` must be specified
    elevation : float, optional
        Scan elevation angle [deg]
    """
    try:
        el = doppler.index.get_level_values('elevation')
    except KeyError:
        assert (elevation is not None), 'PPI scan, need to specify elevation'
        el = elevation
    else:
        assert np.all(el == el[0])
    el = np.radians(el)
    az = np.radians(doppler.index.get_level_values('azimuth'))
    comp1 = np.cos(el)*np.sin(az)
    comp2 = np.cos(el)*np.cos(az)
    missingvals = doppler.isna()
    if np.any(missingvals):
        comp1 = comp1[~missingvals]
        comp2 = comp2[~missingvals]
        doppler = doppler.copy()
        doppler = doppler.loc[~missingvals]
    LHS = np.stack([comp1, comp2], axis=-1)
    from sklearn.linear_model import LinearRegression
    reg = LinearRegression(fit_intercept=False).fit(LHS, doppler)
    U0,V0 = reg.coef_
    Vmag = np.sqrt(U0**2 + V0**2)
    beta = 180. + np.degrees(np.arctan2(U0,V0)) # wind dir follows meteorological convention
    return Vmag, beta


class LidarData(object):
    def __init__(self,df,verbose=True):
        """Lidar data described by range, azimuth, and elevation"""
        self.verbose = verbose
        self.df = df
        self.RHI = False
        self.PPI = False
        self._check_data()

    def _check_data(self):
        # check coordinates
        if all(
            coord in self.df.index.names
            for coord in ['range', 'azimuth', 'elevation']
        ):
            if self.verbose: print('3D volumetric scan loaded')
        elif 'range' not in self.df.index.names:
            if self.verbose: print('Vertical scan loaded')
        elif 'azimuth' not in self.df.index.names:
            if self.verbose: print('RHI scan loaded')
            self.RHI = True
        elif 'elevation' not in self.df.index.names:
            if self.verbose: print('PPI scan loaded')
            self.PPI = True
        else:
            raise IndexError(
                f'Unexpected index levels in dataframe: {str(self.df.index.names)}'
            )
        # check ranges
        rarray = self.df.index.levels[0]
        dr = rarray[1] - rarray[0]
        if hasattr(self, 'range_gate_size'):
            assert self.range_gate_size == dr
        else:
            self.range_gate_size = dr
        self.rmin = rarray[0] - self.range_gate_size/2
        self.rmax = rarray[-1] + self.range_gate_size/2

    @property 
    def r(self):
        return self.df.index.levels[0]

    @property
    def az(self):
        return self.df.index.levels[1]

    @property
    def el(self):
        return self.df.index.levels[2]

    # slicers
    def get(self, r=None, az=None, el=None):
        """Wrapper for get_range, get_azimuth, and get_elevation()"""
        if r is not None:
            return self.get_range(r)
        elif az is not None:
            return self.get_azimuth(az)
        elif el is not None:
            return self.get_elevation(el)
        else:
            print('Specify r, az, or el')

    def get_range(self, r):
        """Get range gate containing request range

        Returns a copy of a multiindex dataframe with (r,az,el) indices
        where r is center of the range gate.
        """
        rarray = self.df.index.levels[0] # center of each range gate
        if r < self.rmin:
            raise ValueError(f'Invalid range, r < {self.rmin}')
        elif r >= self.rmax:
            raise ValueError(f'Invalid range, r >= {self.rmax}')
        if r not in rarray:
            right_edges = rarray + self.range_gate_size/2
            idx = np.where(r < right_edges)[0][0]
            rsel = rarray[idx]
            r0 = rsel - self.range_gate_size/2
            r1 = rsel + self.range_gate_size/2
            assert (r >= r0) & (r < r1), f'{r} is not between {r0} and {r1}'
            if self.verbose:
                print(f'getting nearest range gate {idx} between {r0} and {r1}')
        else:
            rsel = r
            idx = list(rarray).index(r)
            r0 = r - self.range_gate_size/2
            r1 = r + self.range_gate_size/2
            if self.verbose:
                print(f'getting range gate {idx} between {r0} and {r1}')
        #return self.df.xs(rsel, level='range')
        return self.df.loc[(rsel,slice(None),slice(None)),:]
    
    def get_azimuth(self, az):
        """Get requested azimuth (i.e., an RHI slice)

        Returns a copy of a multiindex dataframe with (r,el) indices.
        """
        azarray = self.df.index.levels[1]
        if az < azarray[0]:
            raise ValueError(f'Invalid range, az < {azarray[0]}')
        elif az > azarray[-1]:
            raise ValueError(f'Invalid range, az > {azarray[-1]}')
        if az not in azarray:
            az = azarray[np.argmin(np.abs(az - azarray))]
            if self.verbose:
                print(f'getting nearest azimuth={az} deg')
        return self.df.xs(az, level='azimuth')

    def get_elevation(self, el):
        """Get requested elevation (i.e., a PPI slice)

        Returns a copy of a multiindex dataframe with (r,az) indices.
        """
        elarray = self.df.index.levels[2]
        if el < elarray[0]:
            raise ValueError(f'Invalid range, el < {elarray[0]}')
        elif el > elarray[-1]:
            raise ValueError(f'Invalid range, el > {elarray[-1]}')
        if el not in elarray:
            el = elarray[np.argmin(np.abs(el - elarray))]
            if self.verbose:
                print(f'getting nearest elevation={el} deg')
        return self.df.xs(el, level='elevation')


class GalionCornellPerdigao(LidarData):
    """Data from Galion scanning lidar deployed by Cornell University
    at the Perdigao field campaign

    Tested on data retrieved from the UCAR Earth Observing Laboratory
    data archive (https://data.eol.ucar.edu/dataset/536.036) retrieved
    on 2021-07-24.
    """

    def __init__(self,fpath,load_opts={},**kwargs):
        df = self._load(fpath,**load_opts)
        super().__init__(df, **kwargs)

    def _load(self,
              fpath,
              minimum_range=0.,
              range_gate_size=30.,
              range_gate_name='Range gates',
              azimuth_name='Azimuth angle',
              elevation_name='Elevation angle'):
        """Process a single scan in netcdf format

        Notes:
        - Range gates are stored as integers
        - Not all (r,az,el) data points are available
        """
        ds = xr.open_dataset(fpath)
        assert (len(ds.dims)==1) and ('y' in ds.dims)
        df = ds.to_dataframe()
        df = df.rename(columns={
            range_gate_name: 'range',
            azimuth_name: 'azimuth',
            elevation_name: 'elevation',
        })
        df['range'] = minimum_range + df['range']*range_gate_size
        df['range'] += range_gate_size/2 # shift to center of range gate
        df = df.set_index(['range','azimuth','elevation']).sort_index()
        return df


class GalionCornellPEIWEE(LidarData):
    """Data from Galion scanning lidar deployed by Cornell University
    at the Prince Edward Island Wind Energy Experiment
    """

    def __init__(self,fpath,load_opts={},
                 range_gates=None,
                 ranges=None,
                 intensity_range=(None,None),
                 **kwargs):
        df = self._load(fpath,**load_opts)
        super().__init__(df, **kwargs)
        self._filter_by_range(range_gates=range_gates, ranges=ranges)
        self._filter_by_intensity(*intensity_range)

    def _load(self,
              fpath,
              minimum_range=0.,
              range_gate_size=30.):
        """Process a single scan in netcdf format
        """
        df = fpath.copy() if isinstance(fpath, pd.DataFrame) else pd.read_csv(fpath)
        df['range'] = minimum_range + df['range_gate']*range_gate_size
        df['range'] += range_gate_size/2 # shift to center of range gate
        df = df.set_index(['range','azimuth','elevation']).sort_index()
        return df

    def _filter_by_range(self,range_gates=None,ranges=None):
        assert range_gates or ranges, 'Specify range_gates or ranges'
        allranges = self.df.index.get_level_values('range')
        if range_gates:
            assert isinstance(range_gates, (tuple,list)), \
                    'Specify range_gates as (min_gate, max_gate)'
            if range_gates[0] is not None:
                minrange = allranges[range_gates[0]]
                self.df.loc[allranges <= minrange,:] = np.nan
            if range_gates[1] is not None:
                maxrange = allranges[range_gates[1]]
                self.df.loc[allranges >= maxrange,:] = np.nan
        elif ranges:
            assert isinstance(ranges, (tuple,list)), \
                    'Specify ranges as (min_range, max_range)'
            if ranges[0] is not None:
                self.df.loc[allranges < ranges[0],:] = np.nan
            if ranges[1] is not None:
                self.df.loc[allranges > ranges[1],:] = np.nan

    def _filter_by_intensity(self,min_intensity,max_intensity):
        if min_intensity is not None:
            self.df.loc[self.df['intensity']<min_intensity, :] = np.nan
        if max_intensity is not None:
            self.df.loc[self.df['intensity']>max_intensity, :] = np.nan


